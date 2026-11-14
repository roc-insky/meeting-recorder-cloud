// Task 7 node-level tests: dual-track capture, explicit downgrade, sealing.
//
// The microphone is the recording of last resort. A refused screen share must
// never discard it, but a dual recording that ends up microphone-only is never
// presented as complete dual without an explicit user confirmation.

import assert from "node:assert/strict";
import test from "node:test";

import { ChunkBuffer } from "../../../web/cloud/buffer.mjs";
import { UploadPump } from "../../../web/cloud/upload.mjs";
import { RecordingSession } from "../../../web/cloud/recording.mjs";
import {
  FakeClock,
  FakeMediaDevices,
  FakeUploadTransport,
  MemoryIndexedDB,
  recorderIsAudioOnly,
  waitFor,
} from "./support.mjs";

const USER = "user-a";
const UPLOAD = "upload-1";

let databaseSequence = 0;

async function makeBuffer() {
  databaseSequence += 1;
  const buffer = await ChunkBuffer.open({
    indexedDB: new MemoryIndexedDB(),
    name: `cloud-dual-${databaseSequence}`,
  });
  return buffer;
}

async function makeEnv({ display = null } = {}) {
  const buffer = await makeBuffer();
  const transport = new FakeUploadTransport();
  const pump = new UploadPump({
    buffer,
    transport,
    sleep: async () => {},
    maxAttempts: 1,
  });
  const clock = new FakeClock(0);
  const devices = new FakeMediaDevices({
    mic: { audio: true, sampleRate: 48000 },
    display,
  });
  const session = new RecordingSession({
    userId: USER,
    uploadId: UPLOAD,
    mode: "dual",
    epoch: { id: "epoch-1", offsetMs: 0, anchorMs: 0 },
    mediaDevices: devices,
    buffer,
    pump,
    clock,
  });
  return { buffer, transport, pump, clock, devices, session };
}

async function waitForAcks(env, count) {
  return waitFor(() => env.transport.accepted.length === count);
}

test("missing_system_requires_explicit_downgrade", async () => {
  const env = await makeEnv({ display: null });
  await env.session.start();
  assert.equal(env.devices.recorders.length, 1, "a refused share keeps the microphone recorder");
  assert.equal(env.session.handle.systemAvailable(), false);

  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([1, 2, 3, 4]));
  await waitForAcks(env, 1);

  const first = await env.session.stop();
  assert.equal(first.status, "downgrade_confirmation_required");
  assert.equal(first.seal, null);
  assert.equal(env.transport.sealCalls.length, 0, "no manifest is submitted before confirmation");
  assert.equal(first.downgrade.from, "dual");
  assert.equal(first.downgrade.to, "mic");
  assert.equal(first.downgrade.gap.reason, "missing_track");
  assert.equal(first.downgrade.gap.start_ms, 0);
  assert.ok(first.downgrade.gap.end_ms >= 3000, "the changed interval is recorded");
  assert.equal(first.capture.state, "stopped");

  const confirmed = await env.session.stop({ confirmDowngrade: true });
  assert.equal(env.transport.sealCalls.length, 1);
  const call = env.transport.sealCalls[0];
  assert.deepEqual(
    call.manifest.tracks.map((track) => track.track_id),
    ["mic"],
    "only tracks the server knows about are in the manifest",
  );
  assert.equal(call.manifest.accept_partial, true);
  assert.equal(confirmed.downgrade.to, "mic");
  assert.equal(confirmed.seal.status, "partial");
});

test("video_never_enters_encoder", async () => {
  const env = await makeEnv({ display: { video: true, audio: true } });
  await env.session.start();

  assert.equal(env.devices.recorders.length, 2);
  for (const recorder of env.devices.recorders) {
    assert.equal(recorder.stream.getVideoTracks().length, 0, "no video track reaches a recorder");
    assert.ok(recorderIsAudioOnly(recorder));
  }
  assert.deepEqual(
    env.session.handle.specs().map((spec) => spec.source),
    ["mic", "system"],
  );
  assert.ok(
    env.session.handle.liveTracks().some((track) => track.kind === "video"),
    "the shared video stays live for preview only",
  );
});

test("a_denied_share_can_join_later_on_a_new_user_click", async () => {
  const env = await makeEnv({ display: null });
  await env.session.start();
  assert.equal(env.devices.recorders.length, 1);

  env.devices.display = { video: true, audio: true };
  const joined = await env.session.requestSystem();
  assert.equal(
    env.devices.getDisplayMediaCalls.length,
    2,
    "the denied attempt and the explicit retry are separate user gestures",
  );
  assert.equal(env.devices.recorders.length, 2, "a second explicit click starts the system recorder");
  assert.ok(joined.tracks.some((track) => track.source === "system"));

  env.clock.advance(3000);
  for (const recorder of env.devices.recorders) {
    recorder.emitData(new Uint8Array([5, 6, 7]));
  }
  await waitForAcks(env, 2);

  const snapshot = await env.session.stop();
  assert.equal(env.transport.sealCalls.length, 1);
  assert.equal(snapshot.downgrade, null, "both tracks reached the same point");
  assert.deepEqual(
    env.transport.sealCalls[0].manifest.tracks.map((track) => track.track_id).sort(),
    ["mic", "system"],
  );
  assert.equal(env.transport.sealCalls[0].manifest.accept_partial, false);
});

test("registered_system_track_without_chunks_needs_downgrade", async () => {
  const env = await makeEnv({ display: { video: true, audio: true } });
  await env.session.start();
  assert.equal(env.devices.recorders.length, 2);

  // A track can be present and silent: the recorder exists, but no chunk with
  // system audio ever reaches the buffer.
  env.devices.recorders[1].state = "inactive";

  env.clock.advance(3000);
  env.devices.recorders[0].emitData(new Uint8Array([9, 9, 9]));
  await waitForAcks(env, 1);

  const snapshot = await env.session.stop();
  assert.equal(snapshot.status, "downgrade_confirmation_required");
  assert.equal(snapshot.downgrade.gap.reason, "missing_track");
  assert.equal(env.transport.sealCalls.length, 0);

  const sealed = await env.session.stop({ confirmDowngrade: true });
  const manifest = env.transport.sealCalls[0].manifest;
  const systemEntry = manifest.tracks.find((track) => track.track_id === "system");
  assert.deepEqual(systemEntry, {
    capture_epoch_id: "epoch-1",
    track_id: "system",
    last_sequence: -1,
    end_ms: 0,
  });
  assert.equal(manifest.accept_partial, true);
  assert.equal(sealed.seal.status, "partial");
});
