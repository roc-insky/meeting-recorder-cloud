// Task 6 node-level tests: browser capture, durable buffering, and upload.
//
// Everything here is synthetic: FakeMediaDevices emits WebM-shaped chunks on a
// deterministic clock, and MemoryIndexedDB stands in for the browser store. The
// Playwright suite (buffer.spec.mjs) covers the same contracts against real
// Chromium and real IndexedDB.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  CHUNK_TARGET_MS,
  startCapture,
} from "../../../web/cloud/capture.mjs";
import {
  ChunkBuffer,
  MAX_BUFFERED_BYTES,
  MAX_BUFFERED_MS,
  evaluateQuota,
} from "../../../web/cloud/buffer.mjs";
import { UploadPump, createHttpTransport } from "../../../web/cloud/upload.mjs";
import { RecordingSession } from "../../../web/cloud/recording.mjs";
import {
  CloudRequestError,
  CSRF_HEADER,
  LOGOUT_PATH,
  createCloudSession,
  isSessionExpired,
} from "../../../web/cloud/session.mjs";
import {
  FakeClock,
  FakeMediaDevices,
  FakeUploadTransport,
  MemoryIndexedDB,
  waitFor,
} from "./support.mjs";

const USER = "user-a";
const OTHER_USER = "user-b";
const UPLOAD = "upload-1";
const CODEC = "audio/webm;codecs=opus";

let databaseSequence = 0;

async function makeBuffer(idb = new MemoryIndexedDB()) {
  databaseSequence += 1;
  const name = `cloud-test-${databaseSequence}`;
  const buffer = await ChunkBuffer.open({ indexedDB: idb, name });
  return { idb, name, buffer };
}

function makeMeta(overrides = {}) {
  return {
    uploadSessionId: UPLOAD,
    captureEpochId: "epoch-1",
    epochOffsetMs: 0,
    trackId: "mic",
    sequence: 0,
    startMs: 0,
    endMs: 3000,
    checksum: "0".repeat(64),
    codec: CODEC,
    sampleRate: 48000,
    byteLength: 4,
    ...overrides,
  };
}

async function makeEnv(options = {}) {
  const {
    mode = "mic",
    mic = { audio: true, sampleRate: 48000 },
    display = null,
    failures = 0,
    maxAttempts = 1,
    quota,
    pumpOptions = {},
    buffer: providedBuffer,
  } = options;
  const { idb, name, buffer } = providedBuffer
    ? { idb: new MemoryIndexedDB(), name: "provided", buffer: providedBuffer }
    : await makeBuffer();
  const transport = options.transport ?? new FakeUploadTransport({ failures });
  const pump = new UploadPump({
    buffer,
    transport,
    sleep: async () => {},
    maxAttempts,
    ...pumpOptions,
  });
  const clock = new FakeClock(0);
  const devices = new FakeMediaDevices({ mic, display });
  const closeEvents = {
    calls: 0,
    close() {
      this.calls += 1;
    },
  };
  const session = new RecordingSession({
    userId: USER,
    uploadId: UPLOAD,
    mode,
    epoch: { id: "epoch-1", offsetMs: 0, anchorMs: 0 },
    mediaDevices: devices,
    buffer,
    pump,
    clock,
    closeEvents,
    quota,
  });
  return { idb, name, buffer, transport, pump, clock, devices, closeEvents, session };
}

async function pending(env, userId = USER, uploadId = UPLOAD) {
  return env.buffer.pending(userId, uploadId);
}

async function waitForPending(env, count, userId = USER) {
  return waitFor(async () => (await env.buffer.pending(userId, UPLOAD)).length === count);
}

function activeTracks(devices) {
  return devices.streams.flatMap((stream) => stream.getTracks());
}

test("buffers_before_send_and_clears_only_after_ack", async () => {
  const env = await makeEnv({ failures: 1, maxAttempts: 1 });
  let pendingAtSend = -1;
  env.transport.onSend = async () => {
    pendingAtSend = (await env.buffer.pending(USER, UPLOAD)).length;
  };

  await env.session.start();
  assert.equal(env.devices.recorders.length, 1);
  assert.equal(env.devices.recorders[0].timesliceMs, CHUNK_TARGET_MS);

  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([1, 2, 3, 4]));
  await waitFor(() => env.transport.calls.length === 1);
  await waitFor(async () => (await env.session.snapshot()).upload.state === "buffered_offline");

  assert.equal(pendingAtSend, 1, "chunk must be durable before the network send");
  const retained = await pending(env);
  assert.equal(retained.length, 1);
  assert.equal(retained[0].meta.sequence, 0);
  assert.equal(retained[0].meta.startMs, 0);
  assert.equal(retained[0].meta.endMs, 3000);
  assert.equal(retained[0].meta.codec, CODEC);
  assert.equal(retained[0].meta.checksum.length, 64);
  assert.ok(retained[0].bytes.byteLength > 0);

  const failed = await env.session.snapshot();
  assert.equal(failed.upload.pendingChunks, 1);
  assert.equal(failed.upload.ackedChunks, 0);
  assert.notEqual(failed.upload.state, "complete");

  env.transport.remainingFailures = 0;
  await env.session.resume();
  await waitForPending(env, 0);

  const recovered = await env.session.snapshot();
  assert.equal(recovered.upload.pendingChunks, 0);
  assert.equal(recovered.upload.ackedChunks, 1);
  assert.equal(recovered.upload.state, "complete");
  assert.equal(recovered.upload.lastAck.sequence, 0);
  assert.equal(recovered.capture.state, "recording", "ACK must not stop capture");

  await env.session.stop();
});

test("never_restores_into_other_user", async () => {
  const { idb, name, buffer } = await makeBuffer();
  await buffer.put(USER, makeMeta(), new Uint8Array([9, 9, 9]));
  await buffer.putWarning(USER, UPLOAD, { code: "quota_ms", message: "full", at: 1 });

  assert.equal((await buffer.pending(USER, UPLOAD)).length, 1);
  assert.equal((await buffer.pending(OTHER_USER, UPLOAD)).length, 0);
  assert.equal(await buffer.getWarning(OTHER_USER, UPLOAD), null);
  assert.equal((await buffer.stats(OTHER_USER, UPLOAD)).chunks, 0);

  const reopened = await ChunkBuffer.open({ indexedDB: idb, name });
  assert.equal((await reopened.pending(OTHER_USER, UPLOAD)).length, 0);
  assert.equal((await reopened.pending(USER, UPLOAD)).length, 1);
});

test("quota_stops_without_silent_drop", async () => {
  assert.equal(MAX_BUFFERED_MS, 300000);
  assert.equal(MAX_BUFFERED_BYTES, 100 * 1024 * 1024);

  assert.deepEqual(
    { exceeded: evaluateQuota({ bufferedMs: 299999, bufferedBytes: MAX_BUFFERED_BYTES - 1 }).exceeded },
    { exceeded: false },
  );
  assert.equal(evaluateQuota({ bufferedMs: 300000, bufferedBytes: 1 }).code, "quota_ms");
  assert.equal(evaluateQuota({ bufferedMs: 1, bufferedBytes: MAX_BUFFERED_BYTES }).code, "quota_bytes");
  assert.equal(
    evaluateQuota({ bufferedMs: 300000, bufferedBytes: MAX_BUFFERED_BYTES }).code,
    "quota_ms",
    "milliseconds win the tie",
  );

  const env = await makeEnv({ failures: 999, maxAttempts: 1 });
  await env.session.start();
  const recorder = env.devices.recorders[0];

  for (let index = 0; index < 100; index += 1) {
    env.clock.advance(3000);
    recorder.emitData(new Uint8Array([index % 255, 1, 2, 3]));
    await waitFor(async () => (await env.buffer.pending(USER, UPLOAD)).length >= index + 1);
  }

  await waitFor(async () => (await env.session.snapshot()).upload.warning?.code === "quota_ms");
  await waitFor(() => recorder.state === "inactive");
  await waitFor(() => activeTracks(env.devices).every((track) => track.readyState === "ended"));

  const stats = await env.buffer.stats(USER, UPLOAD);
  const retained = await pending(env);
  const audioChunks = retained.filter((entry) => entry.meta.endMs > entry.meta.startMs);
  assert.equal(stats.bufferedMs, 300000);
  assert.equal(stats.chunks, retained.length);
  assert.equal(audioChunks.length, 100, "every buffered chunk is kept, none silently dropped");
  assert.ok(stats.bytes > 0);
  assert.ok(activeTracks(env.devices).every((track) => track.readyState === "ended"));

  const snap = await env.session.snapshot();
  assert.equal(snap.captureRunning, false);
  assert.equal(snap.upload.state, "quota_stopped");
  assert.equal(snap.upload.warning.code, "quota_ms");

  const restored = await RecordingSession.restore({
    userId: USER,
    uploadId: UPLOAD,
    buffer: env.buffer,
    pump: env.pump,
  });
  const restoredSnap = await restored.snapshot();
  assert.equal(restoredSnap.upload.warning.code, "quota_ms");
  assert.equal(restoredSnap.captureRunning, false);
  assert.equal(restoredSnap.capture.state, "interrupted");
  assert.equal(restoredSnap.upload.pendingChunks, retained.length);
});

test("epoch_clock_reset_keeps_gap", async () => {
  const { buffer } = await makeBuffer();
  const transport = new FakeUploadTransport({ failures: 999 });
  const pump = new UploadPump({ buffer, transport, sleep: async () => {}, maxAttempts: 1 });
  const clock = new FakeClock(0);
  const devices = new FakeMediaDevices();

  const first = new RecordingSession({
    userId: USER,
    uploadId: UPLOAD,
    mode: "mic",
    epoch: { id: "epoch-1", offsetMs: 0, anchorMs: 0 },
    mediaDevices: devices,
    buffer,
    pump,
    clock,
  });
  await first.start();
  for (let index = 0; index < 10; index += 1) {
    clock.advance(3000);
    devices.recorders[0].emitData(new Uint8Array([index, 1, 2]));
    await waitFor(async () => (await buffer.pending(USER, UPLOAD)).length >= index + 1);
  }
  await first.stop();
  await waitFor(async () => (await buffer.pending(USER, UPLOAD)).length >= 11);

  const firstEpoch = (await buffer.pending(USER, UPLOAD)).filter(
    (entry) => entry.meta.captureEpochId === "epoch-1",
  );
  const firstEnd = Math.max(...firstEpoch.map((entry) => entry.meta.endMs));
  assert.equal(firstEnd, 30000);

  const second = new RecordingSession({
    userId: USER,
    uploadId: UPLOAD,
    mode: "mic",
    epoch: { id: "epoch-2", offsetMs: 60000, anchorMs: clock.now() },
    mediaDevices: devices,
    buffer,
    pump,
    clock,
  });
  await second.start();
  clock.advance(3000);
  devices.recorders.at(-1).emitData(new Uint8Array([7, 7, 7]));
  await waitFor(async () =>
    (await buffer.pending(USER, UPLOAD)).some((entry) => entry.meta.captureEpochId === "epoch-2"),
  );
  await second.stop();

  const secondEpoch = (await buffer.pending(USER, UPLOAD)).filter(
    (entry) => entry.meta.captureEpochId === "epoch-2",
  );
  const secondStart = Math.min(...secondEpoch.map((entry) => entry.meta.startMs));
  assert.ok(secondStart >= 60000, `new epoch must start at or after 60000, got ${secondStart}`);
  assert.notEqual(secondStart, 30000);
  assert.equal(secondStart - firstEnd, 30000, "the real gap across epochs is preserved");
  assert.ok(secondEpoch.every((entry) => entry.meta.epochOffsetMs === 60000));
});

test("permission_denied_does_not_start", async () => {
  const denied = Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
  const env = await makeEnv({ mic: { audio: true, error: denied } });
  const states = [];
  env.session.onState = (state) => states.push(state.state ?? state);

  await assert.rejects(() => env.session.start(), /denied|NotAllowed/i);

  const snap = await env.session.snapshot();
  assert.equal(snap.capture.state, "permission_denied");
  assert.equal(snap.captureRunning, false);
  assert.equal(env.devices.recorders.length, 0, "no MediaRecorder exists after denial");
  assert.equal(env.transport.calls.length, 0);
  assert.ok(states.includes("permission_denied"));
});

test("records_true_sample_rate_without_relabeling", async () => {
  const env = await makeEnv({ mic: { audio: true, sampleRate: 48000 }, failures: 999, maxAttempts: 1 });
  await env.session.start();

  const snap = await env.session.snapshot();
  const micTrack = snap.tracks.find((track) => track.source === "mic");
  assert.equal(micTrack.sample_rate, 48000);
  assert.notEqual(micTrack.sample_rate, 16000);
  assert.equal(micTrack.sample_rate_source, "track_settings");

  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([5, 5, 5]));
  await waitForPending(env, 1);

  const [stored] = await pending(env);
  assert.equal(stored.meta.sampleRate, 48000);
  assert.notEqual(stored.meta.sampleRate, 16000);
  assert.equal(stored.meta.codec, CODEC);
  assert.equal(stored.meta.trackId, "mic");
});

test("storage_failure_is_not_reported_as_ack", async () => {
  const env = await makeEnv({ failures: 0 });
  env.buffer.put = async () => {
    const error = new Error("QuotaExceededError: indexeddb write failed");
    error.name = "StorageError";
    throw error;
  };

  await env.session.start();
  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([1, 1, 1]));

  await waitFor(async () => (await env.session.snapshot()).upload.state === "storage_failed");
  await waitFor(() => activeTracks(env.devices).every((track) => track.readyState === "ended"));
  const snap = await env.session.snapshot();
  assert.equal(env.transport.calls.length, 0, "nothing may be sent when persistence failed");
  assert.equal(snap.upload.ackedChunks, 0);
  assert.equal(snap.upload.pendingChunks, 0);
  assert.notEqual(snap.upload.state, "complete");
  assert.equal(snap.upload.warning.code, "storage_failure");
  assert.equal(snap.captureRunning, false);
  assert.ok(activeTracks(env.devices).every((track) => track.readyState === "ended"));
});

test("ack_delete_failure_keeps_chunk_pending", async () => {
  const env = await makeEnv({ failures: 0 });
  const realAck = env.buffer.ack.bind(env.buffer);
  let failedOnce = false;
  env.buffer.ack = async (...args) => {
    if (!failedOnce) {
      failedOnce = true;
      throw new Error("indexeddb delete failed");
    }
    return realAck(...args);
  };

  await env.session.start();
  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([2, 2, 2]));
  await waitFor(() => env.transport.calls.length === 1);
  await waitForPending(env, 1);

  let snap = await env.session.snapshot();
  assert.equal(snap.upload.ackedChunks, 0);
  assert.notEqual(snap.upload.state, "complete");
  assert.equal(snap.upload.pendingChunks, 1);

  await env.session.resume();
  await waitForPending(env, 0);
  snap = await env.session.snapshot();
  assert.equal(snap.upload.ackedChunks, 1);
  assert.equal(snap.upload.state, "complete");
});

test("logout_releases_every_track_and_confirms_abandonment", async () => {
  const env = await makeEnv({
    mode: "dual",
    display: { video: true, audio: true },
    failures: 999,
    maxAttempts: 1,
  });
  await env.session.start();
  assert.equal(env.devices.recorders.length, 2, "per-source recorders");

  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([3, 3, 3]));
  await waitFor(async () => (await env.buffer.pending(USER, UPLOAD)).length >= 1);

  const held = await env.session.logout({ abandon: false });
  assert.equal(held.status, "confirmation_required");
  assert.equal(held.pendingChunks >= 1, true);
  assert.equal(env.closeEvents.calls, 0, "session stays open until the user confirms");
  assert.equal((await env.buffer.pending(USER, UPLOAD)).length >= 1, true);
  assert.ok(
    activeTracks(env.devices).every((track) => track.readyState === "ended"),
    "every mic/system/video track is released before confirmation",
  );

  const done = await env.session.logout({ abandon: true });
  assert.equal(done.status, "logged_out");
  assert.equal(env.closeEvents.calls, 1);
  assert.equal((await env.buffer.pending(USER, UPLOAD)).length, 0);
  assert.equal((await env.buffer.stats(USER, UPLOAD)).chunks, 0);
  assert.equal(await env.buffer.getWarning(USER, UPLOAD), null);

  const snap = await env.session.snapshot();
  assert.equal(snap.upload.state, "abandoned");
  assert.equal(snap.upload.pendingChunks, 0);
  assert.equal(snap.captureRunning, false);
});

test("natural_expiry_preserves_same_owner_buffer", async () => {
  const env = await makeEnv({ failures: 999, maxAttempts: 1 });
  await env.session.start();
  for (let index = 0; index < 2; index += 1) {
    env.clock.advance(3000);
    env.devices.recorders[0].emitData(new Uint8Array([index, 4, 4]));
    await waitFor(async () => (await env.buffer.pending(USER, UPLOAD)).length >= index + 1);
  }

  await env.session.handleSessionExpired();
  const snap = await env.session.snapshot();
  assert.equal(snap.capture.state, "interrupted");
  assert.equal(snap.captureRunning, false);
  assert.equal(snap.upload.warning.code, "session_expired");
  assert.equal(snap.upload.state, "buffered_offline");
  assert.equal(env.closeEvents.calls, 1);
  assert.ok(activeTracks(env.devices).every((track) => track.readyState === "ended"));
  assert.equal((await env.buffer.pending(USER, UPLOAD)).length >= 2, true);
  assert.equal((await env.buffer.pending(OTHER_USER, UPLOAD)).length, 0);

  const transport = new FakeUploadTransport();
  const pump = new UploadPump({ buffer: env.buffer, transport, sleep: async () => {} });
  const restored = await RecordingSession.restore({
    userId: USER,
    uploadId: UPLOAD,
    buffer: env.buffer,
    pump,
  });
  const restoredSnap = await restored.snapshot();
  assert.equal(restoredSnap.upload.warning.code, "session_expired");
  assert.equal(restoredSnap.upload.pendingChunks >= 2, true);
  assert.equal(restoredSnap.captureRunning, false);

  await restored.resume();
  await waitForPending(env, 0);
  const recovered = await restored.snapshot();
  assert.equal(recovered.upload.ackedChunks >= 2, true);
  assert.equal(recovered.upload.pendingChunks, 0);

  await restored.logout({ abandon: true });
  assert.equal((await env.buffer.stats(USER, UPLOAD)).chunks, 0);
  assert.equal(await env.buffer.getWarning(USER, UPLOAD), null);
});

test("refresh_restore_never_claims_capture_is_running", async () => {
  const env = await makeEnv({ failures: 999, maxAttempts: 1 });
  await env.session.start();
  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([6, 6, 6]));
  await waitForPending(env, 1);

  const restored = await RecordingSession.restore({
    userId: USER,
    uploadId: UPLOAD,
    mode: "mic",
    buffer: env.buffer,
    pump: env.pump,
  });
  const snap = await restored.snapshot();
  assert.equal(snap.captureRunning, false);
  assert.equal(snap.capture.state, "interrupted");
  assert.equal(snap.upload.state, "buffered_offline");
  assert.equal(snap.upload.pendingChunks, 1);
  assert.equal(snap.processing.state, "not_submitted");
  assert.deepEqual(snap.tracks, [], "a refresh must not invent live tracks");
});

test("retry_backoff_is_bounded_and_batch_memory_is_bounded", async () => {
  const { buffer } = await makeBuffer();
  const transport = new FakeUploadTransport({ failures: 999 });
  const sleeps = [];
  const pump = new UploadPump({
    buffer,
    transport,
    sleep: async (ms) => sleeps.push(ms),
    maxAttempts: 4,
    backoffMs: [10, 20, 40, 80],
    maxBatch: 3,
  });
  for (let sequence = 0; sequence < 12; sequence += 1) {
    await buffer.put(
      USER,
      makeMeta({ sequence, startMs: sequence * 1000, endMs: (sequence + 1) * 1000 }),
      new Uint8Array([sequence]),
    );
  }

  const limits = [];
  const realPending = buffer.pending.bind(buffer);
  buffer.pending = async (userId, uploadId, options = {}) => {
    limits.push(options.limit);
    return realPending(userId, uploadId, options);
  };

  const failed = await pump.flush(USER, UPLOAD);
  assert.equal(failed.ok, false);
  assert.equal(failed.attempts, 4);
  assert.deepEqual(sleeps, [10, 20, 40], "bounded exponential backoff");
  assert.equal((await buffer.pending(USER, UPLOAD)).length, 12, "failure retains everything");
  assert.equal(transport.calls.length, 4);

  transport.remainingFailures = 0;
  const ok = await pump.flush(USER, UPLOAD);
  assert.equal(ok.ok, true);
  assert.equal(transport.calls.length, 16);
  assert.equal((await buffer.pending(USER, UPLOAD)).length, 0);
  assert.ok(pump.metrics.peakBatch <= 3, `peakBatch=${pump.metrics.peakBatch}`);
  assert.ok(limits.every((limit) => limit === undefined || limit <= 3));
  const sequences = transport.calls.slice(4).map((call) => call.meta.sequence);
  assert.deepEqual(sequences, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
});

test("session_keeps_binary_bodies_and_runs_logout_hook_first", async () => {
  assert.equal(isSessionExpired(new CloudRequestError(401, "unauthenticated", null)), true);
  assert.equal(isSessionExpired(new Error("boom")), false);

  const events = [];
  const blob = new Blob([new Uint8Array([1, 2, 3])], { type: CODEC });
  const cloud = createCloudSession({
    baseUrl: "http://example.test",
    cookieSource: () => "cloud_csrf=token123",
    fetchImpl: async (url, init) => {
      events.push({ url, init });
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await cloud.logout({
    beforeLogout: async () => {
      events.push({ hook: true });
    },
  });
  assert.deepEqual(events.map((event) => event.hook ?? event.url), [
    true,
    "http://example.test/api/cloud/auth/logout",
  ]);
  assert.equal(events[1].init.headers.get(CSRF_HEADER), "token123");

  await cloud.request("/api/cloud/uploads/upload-1/chunks/epoch-1/mic/0", {
    method: "PUT",
    body: blob,
    headers: { "Content-Type": CODEC },
  });
  const put = events.at(-1);
  assert.equal(put.init.body, blob, "Blob bodies pass through untouched");
  assert.equal(put.init.headers.get("Content-Type"), CODEC);
  assert.equal(LOGOUT_PATH, "/api/cloud/auth/logout");
});

test("http_transport_sends_integer_non_overlapping_ranges", async () => {
  const requests = [];
  const transport = createHttpTransport({
    baseUrl: "http://example.test",
    session: {
      async request(url, init) {
        requests.push({ url: new URL(url), init });
        return { accepted: true, duplicate: false, contiguous_ms: 3001, missing: [] };
      },
    },
  });
  const base = {
    uploadSessionId: "upload-1",
    captureEpochId: "epoch-1",
    trackId: "mic",
    checksum: "a".repeat(64),
    codec: CODEC,
  };

  // Fractional performance.now() values are rounded outward for the integer API.
  await transport.putChunk({
    meta: { ...base, sequence: 0, startMs: 0.4, endMs: 3000.6 },
    bytes: new Uint8Array([1]),
  });
  // A zero-duration tail would violate end_ms > start_ms; expanding it must not
  // overlap the following chunk either.
  await transport.putChunk({
    meta: { ...base, sequence: 1, startMs: 3001, endMs: 3001 },
    bytes: new Uint8Array([2]),
  });
  await transport.putChunk({
    meta: { ...base, sequence: 2, startMs: 3001, endMs: 6000.2 },
    bytes: new Uint8Array([3]),
  });
  // Retrying the same sequence must reuse the same wire range.
  await transport.putChunk({
    meta: { ...base, sequence: 2, startMs: 3001, endMs: 6000.2 },
    bytes: new Uint8Array([3]),
  });

  const ranges = requests.map(({ url }) => [
    url.searchParams.get("start_ms"),
    url.searchParams.get("end_ms"),
  ]);
  assert.deepEqual(ranges, [
    ["0", "3001"],
    ["3001", "3002"],
    ["3002", "6001"],
    ["3002", "6001"],
  ]);
  for (const [index, { url, init }] of requests.entries()) {
    assert.equal(url.pathname, `/api/cloud/uploads/upload-1/chunks/epoch-1/mic/${index === 3 ? 2 : index}`);
    assert.equal(url.searchParams.get("checksum"), "a".repeat(64));
    assert.equal(url.searchParams.get("codec"), CODEC);
    assert.equal(init.method, "PUT");
    assert.ok(init.body instanceof Uint8Array);
  }
});

test("no_service_worker_is_registered", async () => {
  const files = ["capture.mjs", "buffer.mjs", "upload.mjs", "recording.mjs", "session.mjs"];
  for (const file of files) {
    const source = await readFile(new URL(`../../../web/cloud/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /serviceWorker\s*\.\s*register/);
  }
});
