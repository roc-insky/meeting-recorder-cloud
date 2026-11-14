// Real-browser IndexedDB + MediaRecorder contracts for Task 6.
//
// The uploader is a fake that lives inside the page; the storage, the recorder,
// and the microphone pipeline are real Chromium. No server is contacted.

import { expect, test } from "@playwright/test";

const FIXTURE = "/tests/browser/cloud/fixture.html";
const CHECKSUM = "a".repeat(64);

function meta(overrides = {}) {
  return {
    uploadSessionId: "upload-1",
    captureEpochId: "epoch-1",
    epochOffsetMs: 0,
    trackId: "mic",
    sequence: 0,
    startMs: 0,
    endMs: 3000,
    checksum: CHECKSUM,
    codec: "audio/webm;codecs=opus",
    sampleRate: 48000,
    byteLength: 3,
    ...overrides,
  };
}

test("persists a chunk before the uploader sees it and clears only after ACK", async ({ page }) => {
  await page.goto(FIXTURE);
  const result = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const { UploadPump } = await import("/web/cloud/upload.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-order" });
    const calls = [];
    let pendingAtSend = -1;
    const transport = {
      async putChunk({ meta }) {
        calls.push(meta.sequence);
        pendingAtSend = (await buffer.pending("user-a", "upload-1")).length;
        if (calls.length === 1) throw new Error("network_down");
        return { accepted: true, duplicate: false, contiguous_ms: meta.endMs, missing: [] };
      },
    };
    const pump = new UploadPump({ buffer, transport, sleep: async () => {}, maxAttempts: 1 });
    const chunk = {
      uploadSessionId: "upload-1",
      captureEpochId: "epoch-1",
      epochOffsetMs: 0,
      trackId: "mic",
      sequence: 0,
      startMs: 0,
      endMs: 3000,
      checksum: "a".repeat(64),
      codec: "audio/webm;codecs=opus",
      sampleRate: 48000,
      byteLength: 3,
    };
    await buffer.put("user-a", chunk, new Uint8Array([1, 2, 3]));
    const failed = await pump.flush("user-a", "upload-1");
    const afterFailure = (await buffer.pending("user-a", "upload-1")).length;
    const recovered = await pump.flush("user-a", "upload-1");
    const afterAck = (await buffer.pending("user-a", "upload-1")).length;
    return { pendingAtSend, failedOk: failed.ok, afterFailure, recoveredOk: recovered.ok, afterAck };
  });
  expect(result.pendingAtSend).toBe(1);
  expect(result.failedOk).toBe(false);
  expect(result.afterFailure).toBe(1);
  expect(result.recoveredOk).toBe(true);
  expect(result.afterAck).toBe(0);
});

test("owner scoping survives a real reload", async ({ page }) => {
  await page.goto(FIXTURE);
  const written = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-reload" });
    await buffer.put(
      "user-a",
      {
        uploadSessionId: "upload-1",
        captureEpochId: "epoch-1",
        epochOffsetMs: 0,
        trackId: "mic",
        sequence: 0,
        startMs: 0,
        endMs: 3000,
        checksum: "b".repeat(64),
        codec: "audio/webm;codecs=opus",
        sampleRate: 48000,
        byteLength: 3,
      },
      new Uint8Array([4, 5, 6]),
    );
    await buffer.putWarning("user-a", "upload-1", { code: "session_expired", message: "expired", at: 7 });
    return (await buffer.pending("user-a", "upload-1")).length;
  });
  expect(written).toBe(1);

  await page.reload();

  const afterReload = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-reload" });
    return {
      owner: (await buffer.pending("user-a", "upload-1")).length,
      stranger: (await buffer.pending("user-b", "upload-1")).length,
      warning: await buffer.getWarning("user-b", "upload-1"),
    };
  });
  expect(afterReload.owner).toBe(1);
  expect(afterReload.stranger).toBe(0);
  expect(afterReload.warning).toBeNull();
});

test("real MediaRecorder chunk is durable with its true sample rate", async ({ page }) => {
  await page.goto(FIXTURE);
  const result = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const { UploadPump } = await import("/web/cloud/upload.mjs");
    const { RecordingSession } = await import("/web/cloud/recording.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-mic" });
    const calls = [];
    const transport = {
      async putChunk({ meta, bytes }) {
        calls.push({
          sequence: meta.sequence,
          startMs: meta.startMs,
          endMs: meta.endMs,
          size: bytes.byteLength,
          sampleRate: meta.sampleRate,
          codec: meta.codec,
        });
        throw new Error("offline");
      },
    };
    const pump = new UploadPump({ buffer, transport, sleep: async () => {}, maxAttempts: 1 });
    const session = new RecordingSession({
      userId: "user-a",
      uploadId: "upload-1",
      mode: "mic",
      epoch: { id: "epoch-1", offsetMs: 0, anchorMs: performance.now() },
      mediaDevices: navigator.mediaDevices,
      buffer,
      pump,
      chunkTargetMs: 400,
    });
    await session.start();
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline && calls.length === 0) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const started = await session.snapshot();
    await session.stop();
    const stored = await buffer.pending("user-a", "upload-1");
    return {
      calls,
      stored: stored.map((entry) => ({
        sequence: entry.meta.sequence,
        startMs: entry.meta.startMs,
        endMs: entry.meta.endMs,
        sampleRate: entry.meta.sampleRate,
        codec: entry.meta.codec,
        bytes: entry.bytes.byteLength,
      })),
      tracks: started.tracks,
      captureState: started.capture.state,
    };
  });

  expect(result.calls.length).toBeGreaterThanOrEqual(1);
  expect(result.stored.length).toBeGreaterThanOrEqual(1);
  for (const chunk of result.stored) {
    expect(chunk.sampleRate).toBe(48000);
    expect(chunk.codec).toContain("webm");
    expect(chunk.bytes).toBeGreaterThan(0);
    expect(chunk.endMs).toBeGreaterThan(chunk.startMs);
  }
  expect(result.tracks[0].source).toBe("mic");
  expect(result.tracks[0].sample_rate).toBe(48000);
});

test("permission denial never starts capture or storage", async ({ page }) => {
  await page.addInitScript(() => {
    const mediaDevices = navigator.mediaDevices;
    mediaDevices.getUserMedia = async () => {
      throw new DOMException("Permission denied", "NotAllowedError");
    };
  });
  await page.goto(FIXTURE);
  const result = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const { UploadPump } = await import("/web/cloud/upload.mjs");
    const { RecordingSession } = await import("/web/cloud/recording.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-denied" });
    const calls = [];
    const transport = {
      async putChunk() {
        calls.push(1);
        return { accepted: true, duplicate: false, contiguous_ms: 0, missing: [] };
      },
    };
    const pump = new UploadPump({ buffer, transport, sleep: async () => {}, maxAttempts: 1 });
    const session = new RecordingSession({
      userId: "user-a",
      uploadId: "upload-1",
      mode: "mic",
      epoch: { id: "epoch-1", offsetMs: 0, anchorMs: 0 },
      mediaDevices: navigator.mediaDevices,
      buffer,
      pump,
    });
    let rejected = false;
    try {
      await session.start();
    } catch {
      rejected = true;
    }
    const snap = await session.snapshot();
    return {
      rejected,
      state: snap.capture.state,
      running: snap.captureRunning,
      pending: (await buffer.pending("user-a", "upload-1")).length,
      calls: calls.length,
    };
  });
  expect(result.rejected).toBe(true);
  expect(result.state).toBe("permission_denied");
  expect(result.running).toBe(false);
  expect(result.pending).toBe(0);
  expect(result.calls).toBe(0);
});

test("logout releases every real track and clears the buffer", async ({ page }) => {
  await page.goto(FIXTURE);
  const result = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const { UploadPump } = await import("/web/cloud/upload.mjs");
    const { RecordingSession } = await import("/web/cloud/recording.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-logout" });
    const transport = { async putChunk() { throw new Error("offline"); } };
    const pump = new UploadPump({ buffer, transport, sleep: async () => {}, maxAttempts: 1 });
    const session = new RecordingSession({
      userId: "user-a",
      uploadId: "upload-1",
      mode: "mic",
      epoch: { id: "epoch-1", offsetMs: 0, anchorMs: performance.now() },
      mediaDevices: navigator.mediaDevices,
      buffer,
      pump,
      chunkTargetMs: 300,
    });
    await session.start();
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      const pending = await buffer.pending("user-a", "upload-1");
      if (pending.length > 0) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const before = await session.snapshot();
    const first = await session.logout({ abandon: false });
    const afterStop = await session.snapshot();
    const second = await session.logout({ abandon: true });
    const pending = await buffer.pending("user-a", "upload-1");
    return {
      beforeLive: before.tracks.map((track) => track.readyState),
      first,
      afterStopLive: afterStop.tracks.map((track) => track.readyState),
      second,
      pending: pending.length,
    };
  });
  expect(result.beforeLive.every((state) => state === "live")).toBe(true);
  expect(result.first.status).toBe("confirmation_required");
  expect(result.afterStopLive.every((state) => state === "ended")).toBe(true);
  expect(result.second.status).toBe("logged_out");
  expect(result.pending).toBe(0);
});

test("refresh restore reports interruption, never running capture", async ({ page }) => {
  await page.goto(FIXTURE);
  await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-refresh" });
    await buffer.put(
      "user-a",
      {
        uploadSessionId: "upload-1",
        captureEpochId: "epoch-1",
        epochOffsetMs: 0,
        trackId: "mic",
        sequence: 0,
        startMs: 0,
        endMs: 3000,
        checksum: "c".repeat(64),
        codec: "audio/webm;codecs=opus",
        sampleRate: 48000,
        byteLength: 3,
      },
      new Uint8Array([7, 8, 9]),
    );
  });

  await page.reload();

  const result = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const { UploadPump } = await import("/web/cloud/upload.mjs");
    const { RecordingSession } = await import("/web/cloud/recording.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-refresh" });
    const pump = new UploadPump({
      buffer,
      transport: { async putChunk() { return { accepted: true, duplicate: false, contiguous_ms: 3000, missing: [] }; } },
      sleep: async () => {},
    });
    const session = await RecordingSession.restore({
      userId: "user-a",
      uploadId: "upload-1",
      mode: "mic",
      buffer,
      pump,
    });
    const snap = await session.snapshot();
    return {
      running: snap.captureRunning,
      captureState: snap.capture.state,
      uploadState: snap.upload.state,
      pending: snap.upload.pendingChunks,
      tracks: snap.tracks.length,
    };
  });
  expect(result.running).toBe(false);
  expect(result.captureState).toBe("interrupted");
  expect(result.uploadState).toBe("buffered_offline");
  expect(result.pending).toBe(1);
  expect(result.tracks).toBe(0);
});

test("quota warning survives a reload", async ({ page }) => {
  await page.goto(FIXTURE);
  const result = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-quota" });
    for (let sequence = 0; sequence < 100; sequence += 1) {
      await buffer.put(
        "user-a",
        {
          uploadSessionId: "upload-1",
          captureEpochId: "epoch-1",
          epochOffsetMs: 0,
          trackId: "mic",
          sequence,
          startMs: sequence * 3000,
          endMs: (sequence + 1) * 3000,
          checksum: "d".repeat(64),
          codec: "audio/webm;codecs=opus",
          sampleRate: 48000,
          byteLength: 3,
        },
        new Uint8Array([1, 2, 3]),
      );
    }
    await buffer.putWarning("user-a", "upload-1", { code: "quota_ms", message: "full", at: 9 });
    return (await buffer.stats("user-a", "upload-1")).bufferedMs;
  });
  expect(result).toBe(300000);

  await page.reload();

  const warning = await page.evaluate(async () => {
    const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
    const buffer = await ChunkBuffer.open({ name: "spec-quota" });
    return buffer.getWarning("user-a", "upload-1");
  });
  expect(warning.code).toBe("quota_ms");
});

test("fixture page never registers a service worker", async ({ page }) => {
  const registrations = [];
  page.on("console", (message) => {
    if (message.text().includes("serviceWorker.register")) registrations.push(message.text());
  });
  await page.goto(FIXTURE);
  const count = await page.evaluate(async () => {
    if (!("serviceWorker" in navigator)) return 0;
    return (await navigator.serviceWorker.getRegistrations()).length;
  });
  expect(count).toBe(0);
  expect(registrations).toEqual([]);
});
