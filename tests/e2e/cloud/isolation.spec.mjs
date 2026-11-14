// Task 16 browser isolation: private state must stay inside one browser
// context, and an expired or throttled session must never look successful.
//
// Everything here is mocked at the browser network boundary (page.route) and
// the capture devices are fake. No real backend, provider, microphone, audio
// device or IndexedDB migration is involved. The two browser contexts are real
// Chromium contexts, so their cookies and storage are genuinely separate.

import { expect, test } from "@playwright/test";

const ORIGIN = "http://127.0.0.1:" + (process.env.CLOUD_TEST_PORT ?? "43119");
const CSRF_TOKEN = "test-csrf-token";
const WORKBENCH = "/web/cloud/index.html?meeting=";
const SHARED_DATABASE = "cloud-recorder-isolation-shared";

function silentWav(seconds = 1) {
  const sampleRate = 16000;
  const samples = sampleRate * seconds;
  const wav = Buffer.alloc(44 + samples * 2);
  wav.write("RIFF", 0);
  wav.writeUInt32LE(36 + samples * 2, 4);
  wav.write("WAVE", 8);
  wav.write("fmt ", 12);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(sampleRate, 24);
  wav.writeUInt32LE(sampleRate * 2, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36);
  wav.writeUInt32LE(samples * 2, 40);
  return wav;
}

function makeMeeting(account) {
  return {
    id: account.meetingId,
    title: account.title,
    mode: "mic",
    generation: 1,
    current_transcript_id: "t-" + account.meetingId,
    deleted_at: null,
  };
}

function makeTranscript(account) {
  return {
    id: "t-" + account.meetingId,
    meeting_id: account.meetingId,
    status: "complete",
    empty: false,
    warnings: [],
    speakers: [{ id: "spk1", label: "本人", suspect: false }],
    segments: [
      {
        id: "s1",
        start_ms: 0,
        end_ms: 1200,
        text: account.transcriptText,
        speaker_id: "spk1",
        track_id: "mic",
      },
    ],
  };
}

async function mockApi(page, account) {
  const state = {
    chunkRequests: 0,
    sealRequests: 0,
    voiceprintRequests: 0,
    lastChunkPath: null,
    lastChunkUser: null,
  };
  const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/api/cloud/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/cloud/auth/session") {
      await json(route, {
        user_id: account.userId,
        csrf_token: CSRF_TOKEN,
        expires_at: "2030-01-01T00:00:00Z",
      });
      return;
    }

    if (path === "/api/cloud/meetings" && method === "GET") {
      await json(route, { items: [makeMeeting(account)], next_cursor: null });
      return;
    }

    if (path === "/api/cloud/voiceprints" && method === "GET") {
      state.voiceprintRequests += 1;
      await json(route, [
        {
          id: account.profileId,
          name: "张三",
          version: 1,
          metadata: {
            model_id: "ecapa-tdnn",
            model_version: "2026.05",
            weights_sha256: "abc123",
            dimension: 192,
            sample_rate: 16000,
            preprocess_version: "p1",
            normalization_version: "n1",
            calibration_version: "c1",
          },
          revoked: false,
          created_at: "2026-05-01T08:00:00Z",
          captured_at: "2026-05-01T07:58:00Z",
          owner_note: account.userId,
        },
      ]);
      return;
    }

    const meeting = makeMeeting(account);
    const encoded = encodeURIComponent(meeting.id);
    if (path === "/api/cloud/meetings/" + encoded && method === "GET") {
      await json(route, meeting);
      return;
    }
    if (path === "/api/cloud/meetings/" + encoded + "/transcripts" && method === "GET") {
      await json(route, [makeTranscript(account)]);
      return;
    }
    if (path === "/api/cloud/meetings/" + encoded + "/annotations" && method === "GET") {
      await json(route, []);
      return;
    }
    if (path === "/api/cloud/meetings/" + encoded + "/audio" && method === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "audio/wav",
        headers: { "Accept-Ranges": "bytes", "Cache-Control": "no-store" },
        body: silentWav(1),
      });
      return;
    }

    if (/^\/api\/cloud\/uploads\/[^/]+\/chunks\//.test(path) && method === "PUT") {
      state.chunkRequests += 1;
      state.lastChunkPath = path;
      state.lastChunkUser = account.userId;
      await json(route, {
        accepted: true,
        duplicate: false,
        contiguous_ms: account.baseContiguousMs + state.chunkRequests * 1000,
        missing: [],
      });
      return;
    }
    if (/^\/api\/cloud\/uploads\/[^/]+\/seal$/.test(path) && method === "POST") {
      state.sealRequests += 1;
      await json(route, { status: "complete", input_version: 1, gaps: [] });
      return;
    }

    await json(route, { error: "not_found" }, 404);
  });
  return state;
}

async function installFakeCapture(page) {
  await page.addInitScript(() => {
    window.__fakeCapture = { recorders: [], getUserMediaCalls: 0, chunks: 0 };

    // capture.mjs wraps each selected track in `new MediaStream([track])` when
    // the browser global exists. Its tracks are plain fakes here, so provide a
    // minimal MediaStream-shaped replacement instead of a real Chromium track.
    class FakeMediaStream {
      constructor(tracks = []) {
        this.tracks = tracks.slice();
      }
      getTracks() {
        return this.tracks.slice();
      }
      getAudioTracks() {
        return this.tracks.filter((track) => track.kind === "audio");
      }
      getVideoTracks() {
        return this.tracks.filter((track) => track.kind === "video");
      }
    }
    try {
      Object.defineProperty(globalThis, "MediaStream", {
        configurable: true,
        writable: true,
        value: FakeMediaStream,
      });
    } catch {
      // The fake recorder is still driven through createRecorder below.
    }

    function fakeTrack() {
      return {
        kind: "audio",
        label: "fake-microphone",
        readyState: "live",
        enabled: true,
        muted: false,
        stopped: false,
        stop() {
          this.stopped = true;
          this.readyState = "ended";
        },
        addEventListener() {},
        removeEventListener() {},
        getSettings() {
          return { sampleRate: 48000 };
        },
      };
    }

    function fakeStream() {
      const tracks = [fakeTrack()];
      return {
        active: true,
        getTracks: () => tracks.slice(),
        getAudioTracks: () => tracks.slice(),
        getVideoTracks: () => [],
      };
    }

    class FakeCaptureRecorder {
      static isTypeSupported(type) {
        return ["audio/webm;codecs=opus", "audio/webm", "audio/ogg", "audio/mp4"].includes(type);
      }
      constructor(stream, options = {}) {
        this.stream = stream;
        this.mimeType = options.mimeType || "audio/webm;codecs=opus";
        this.state = "inactive";
        this.ondataavailable = null;
        this.onstop = null;
        this.onerror = null;
        this.timer = null;
        window.__fakeCapture.recorders.push(this);
      }
      emit() {
        if (this.state !== "recording" || !this.ondataavailable) return;
        window.__fakeCapture.chunks += 1;
        this.ondataavailable({
          data: new Blob(["fake-audio-" + window.__fakeCapture.chunks], {
            type: this.mimeType.split(";")[0],
          }),
        });
      }
      start(timeslice) {
        if (this.state !== "inactive") throw new Error("recorder already started");
        this.state = "recording";
        const interval = Math.max(10, Math.min(40, Number(timeslice) || 30));
        this.timer = setInterval(() => this.emit(), interval);
      }
      stop() {
        if (this.state === "inactive") {
          if (this.onstop) this.onstop(new Event("stop"));
          return;
        }
        this.state = "inactive";
        clearInterval(this.timer);
        this.emit();
        setTimeout(() => {
          if (this.onstop) this.onstop(new Event("stop"));
        }, 0);
      }
    }

    window.__FakeCaptureRecorder = FakeCaptureRecorder;
    window.__fakeMediaDevices = {
      async getUserMedia() {
        window.__fakeCapture.getUserMediaCalls += 1;
        return fakeStream();
      },
      async getDisplayMedia() {
        return fakeStream();
      },
      createRecorder(stream, options) {
        return new FakeCaptureRecorder(stream, options);
      },
    };
  });
}

async function openAccount(browser, account) {
  const context = await browser.newContext();
  await context.addCookies([{ name: "cloud_csrf", value: CSRF_TOKEN, url: ORIGIN }]);
  const page = await context.newPage();
  const state = await mockApi(page, account);
  await installFakeCapture(page);
  await page.goto(ORIGIN + WORKBENCH + encodeURIComponent(account.meetingId));
  await expect(page.getByTestId("workbench")).toBeVisible();
  await expect(page.getByTestId("transcript-segment").first()).toContainText(account.transcriptText);
  return { context, page, state };
}

function account(overrides) {
  return {
    userId: "user-aaaaaaaaaaaaaaaa",
    meetingId: "m-a",
    title: "A 的会议",
    transcriptText: "A 的私有文字稿",
    profileId: "vp-a",
    baseContiguousMs: 1000,
    ...overrides,
  };
}

async function runRecordingSession(page, config) {
  return page.evaluate(async (options) => {
    const [{ ChunkBuffer }, { RecordingSession }, { UploadPump, createHttpTransport }, { createCloudSession }] =
      await Promise.all([
        import("/web/cloud/buffer.mjs"),
        import("/web/cloud/recording.mjs"),
        import("/web/cloud/upload.mjs"),
        import("/web/cloud/session.mjs"),
      ]);
    const buffer = await ChunkBuffer.open({ name: options.databaseName });
    await buffer.clearOwner(options.userId);
    const client = createCloudSession({ baseUrl: globalThis.location.origin });
    const pump = new UploadPump({
      buffer,
      transport: createHttpTransport({ session: client }),
      maxAttempts: 2,
      backoffMs: [1],
      sleep: async () => {},
    });
    let tick = 0;
    const clock = { now: () => (tick += 250) };
    const session = new RecordingSession({
      userId: options.userId,
      uploadId: options.uploadId,
      mode: "mic",
      mediaDevices: window.__fakeMediaDevices,
      buffer,
      pump,
      clock,
      chunkTargetMs: 25,
      captureOptions: { RecorderCtor: window.__FakeCaptureRecorder },
    });
    await session.start();
    for (let index = 0; index < 150; index += 1) {
      const snapshot = await session.snapshot();
      if (snapshot.upload.ackedChunks > 0) break;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const stream = {
      closed: 0,
      close() {
        this.closed += 1;
      },
    };
    session.attachEventStream(stream);
    session.applyServerEvent({
      event: "transcript.published",
      cursor: 7,
      data: { version_id: options.transcriptVersion },
    });
    const snapshot = await session.snapshot();
    const buffered = await buffer.stats(options.userId, options.uploadId);
    const recording = await session.stop();
    session.closeEventStream();
    session.buffer.close?.();
    return {
      snapshot,
      buffered,
      recording,
      streamClosed: stream.closed,
      tracks: snapshot.tracks.map((track) => ({ source: track.source, readyState: track.readyState })),
      captureChunks: window.__fakeCapture.chunks,
    };
  }, config);
}

test("two browser contexts keep recording, ACK, events, transcripts and voiceprints private", async ({ browser }) => {
  const accountA = account({});
  const accountB = account({
    userId: "user-bbbbbbbbbbbbbbbb",
    meetingId: "m-b",
    title: "B 的会议",
    transcriptText: "B 的私有文字稿",
    profileId: "vp-b",
    baseContiguousMs: 9000,
  });
  const a = await openAccount(browser, accountA);
  const b = await openAccount(browser, accountB);

  try {
    // The rendered workbench is already account-scoped by its own mock.
    await expect(a.page.getByTestId("transcript-segment").first()).toContainText("A 的私有文字稿");
    await expect(b.page.getByTestId("transcript-segment").first()).toContainText("B 的私有文字稿");
    await expect(a.page.locator("body")).not.toContainText("B 的私有文字稿");
    await expect(b.page.locator("body")).not.toContainText("A 的私有文字稿");

    const recordedA = await runRecordingSession(a.page, {
      userId: accountA.userId,
      uploadId: "upload-a",
      transcriptVersion: "t-a-private",
      databaseName: SHARED_DATABASE,
    });
    const recordedB = await runRecordingSession(b.page, {
      userId: accountB.userId,
      uploadId: "upload-b",
      transcriptVersion: "t-b-private",
      databaseName: SHARED_DATABASE,
    });

    expect(recordedA.snapshot.captureRunning).toBe(true);
    expect(recordedA.snapshot.upload.ackedChunks).toBeGreaterThan(0);
    expect(recordedA.buffered.chunks).toBe(0);
    expect(recordedA.tracks).toEqual([{ source: "mic", readyState: "live" }]);
    expect(recordedA.snapshot.processing).toMatchObject({
      state: "completed",
      transcriptId: "t-a-private",
    });
    expect(recordedA.streamClosed).toBe(1);
    expect(recordedA.captureChunks).toBeGreaterThan(0);

    expect(recordedB.snapshot.captureRunning).toBe(true);
    expect(recordedB.snapshot.upload.ackedChunks).toBeGreaterThan(0);
    expect(recordedB.buffered.chunks).toBe(0);
    expect(recordedB.snapshot.processing).toMatchObject({
      state: "completed",
      transcriptId: "t-b-private",
    });
    expect(recordedB.streamClosed).toBe(1);
    expect(recordedB.captureChunks).toBeGreaterThan(0);

    expect(a.state.chunkRequests).toBeGreaterThan(0);
    expect(b.state.chunkRequests).toBeGreaterThan(0);
    expect(a.state.lastChunkUser).toBe(accountA.userId);
    expect(b.state.lastChunkUser).toBe(accountB.userId);
    expect(a.state.lastChunkPath).toContain("/upload-a/");
    expect(b.state.lastChunkPath).toContain("/upload-b/");
    expect(a.state.lastChunkPath).not.toBe(b.state.lastChunkPath);

    // Same display name, different actual profile: the API response is scoped
    // by the browser context and neither page can read the other's profile id.
    const profilesA = await a.page.evaluate(() =>
      fetch("/api/cloud/voiceprints").then((response) => response.json()),
    );
    const profilesB = await b.page.evaluate(() =>
      fetch("/api/cloud/voiceprints").then((response) => response.json()),
    );
    expect(profilesA).toHaveLength(1);
    expect(profilesB).toHaveLength(1);
    expect(profilesA[0].name).toBe("张三");
    expect(profilesB[0].name).toBe("张三");
    expect(profilesA[0].id).toBe("vp-a");
    expect(profilesB[0].id).toBe("vp-b");
    expect(profilesA[0].owner_note).toBe(accountA.userId);
    expect(profilesB[0].owner_note).toBe(accountB.userId);
    expect(a.state.voiceprintRequests).toBe(1);
    expect(b.state.voiceprintRequests).toBe(1);
  } finally {
    await a.context.close();
    await b.context.close();
  }
});

test("logout clears only that account's buffer, closes its EventSource and rejects late writes", async ({ browser }) => {
  const sharedUser = "user-shared-000000000";
  const sharedUpload = "upload-shared";
  const a = await openAccount(browser, account({ userId: sharedUser }));
  const b = await openAccount(browser, account({ userId: sharedUser }));

  try {
    const setupPending = async (page, marker) =>
      page.evaluate(
        async (options) => {
          const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
          const { RecordingSession } = await import("/web/cloud/recording.mjs");
          const buffer = await ChunkBuffer.open({ name: options.databaseName });
          await buffer.clearOwner(options.userId);
          const session = new RecordingSession({
            userId: options.userId,
            uploadId: options.uploadId,
            mode: "mic",
            buffer,
            pump: {
              flush: async () => ({
                ok: false,
                acked: 0,
                pending: 1,
                error: { message: "offline", name: "UploadError" },
              }),
              transport: null,
            },
            clock: { now: () => 1000 },
          });
          await session.handleChunk({
            meta: {
              trackId: "mic",
              source: "mic",
              sequence: 0,
              startMs: 0,
              endMs: 1000,
              codec: "audio/webm",
              sampleRate: 48000,
            },
            bytes: new Uint8Array([1, 2, 3, 4]),
            trackSpec: { codec: "audio/webm", source: "mic", sample_rate: 48000 },
          });
          const stream = {
            closed: 0,
            close() {
              this.closed += 1;
            },
          };
          session.attachEventStream(stream);
          window.__isolation = { buffer, session, stream };
          return {
            status: options.marker,
            pending: (await buffer.stats(options.userId, options.uploadId)).chunks,
            streamClosed: stream.closed,
            uploadState: (await session.snapshot()).upload.state,
          };
        },
        { userId: sharedUser, uploadId: sharedUpload, databaseName: SHARED_DATABASE, marker },
      );

    // Both contexts write the same owner + upload key. If browser storage were
    // shared, the first logout below would erase the second context's chunk.
    const beforeB = await setupPending(b.page, "b");
    expect(beforeB.pending).toBe(1);
    expect(beforeB.streamClosed).toBe(0);
    expect(beforeB.uploadState).toBe("buffered_offline");

    const afterLogoutA = await a.page.evaluate(
      async (options) => {
        const { ChunkBuffer } = await import("/web/cloud/buffer.mjs");
        const { RecordingSession } = await import("/web/cloud/recording.mjs");
        const buffer = await ChunkBuffer.open({ name: options.databaseName });
        await buffer.clearOwner(options.userId);
        const session = new RecordingSession({
          userId: options.userId,
          uploadId: options.uploadId,
          mode: "mic",
          buffer,
          pump: {
            flush: async () => ({
              ok: false,
              acked: 0,
              pending: 1,
              error: { message: "offline", name: "UploadError" },
            }),
            transport: null,
          },
          clock: { now: () => 2000 },
        });
        await session.handleChunk({
          meta: {
            trackId: "mic",
            source: "mic",
            sequence: 0,
            startMs: 0,
            endMs: 1000,
            codec: "audio/webm",
            sampleRate: 48000,
          },
          bytes: new Uint8Array([5, 6, 7, 8]),
          trackSpec: { codec: "audio/webm", source: "mic", sample_rate: 48000 },
        });
        const stream = {
          closed: 0,
          close() {
            this.closed += 1;
          },
        };
        session.attachEventStream(stream);
        const beforeLogout = await buffer.stats(options.userId, options.uploadId);
        const logout = await session.logout({ abandon: true });
        const afterLogout = await buffer.stats(options.userId, options.uploadId);
        const lateStream = {
          closed: 0,
          close() {
            this.closed += 1;
          },
        };
        const attached = session.attachEventStream(lateStream);
        let lateChunkError = null;
        try {
          await session.handleChunk({
            meta: {
              trackId: "mic",
              source: "mic",
              sequence: 1,
              startMs: 1000,
              endMs: 2000,
              codec: "audio/webm",
              sampleRate: 48000,
            },
            bytes: new Uint8Array([9, 10]),
            trackSpec: { codec: "audio/webm", source: "mic", sample_rate: 48000 },
          });
        } catch (error) {
          lateChunkError = error?.message ?? String(error);
        }
        const afterLate = await buffer.stats(options.userId, options.uploadId);
        buffer.close?.();
        return {
          beforeLogout: beforeLogout.chunks,
          afterLogout: afterLogout.chunks,
          afterLate: afterLate.chunks,
          logoutStatus: logout.status,
          streamClosed: stream.closed,
          lateStreamClosed: lateStream.closed,
          lateAttached: attached,
          lateChunkError,
        };
      },
      { userId: sharedUser, uploadId: sharedUpload, databaseName: SHARED_DATABASE },
    );

    expect(afterLogoutA).toMatchObject({
      beforeLogout: 1,
      afterLogout: 0,
      afterLate: 0,
      logoutStatus: "logged_out",
      streamClosed: 1,
      lateStreamClosed: 1,
      lateAttached: null,
    });
    expect(afterLogoutA.lateChunkError).toBe("session_logged_out");

    const afterB = await b.page.evaluate(async (options) => {
      const state = window.__isolation;
      const stats = await state.buffer.stats(options.userId, options.uploadId);
      return {
        pending: stats.chunks,
        streamClosed: state.stream.closed,
        uploadState: (await state.session.snapshot()).upload.state,
      };
    }, { userId: sharedUser, uploadId: sharedUpload });
    expect(afterB).toEqual({ pending: 1, streamClosed: 0, uploadState: "buffered_offline" });
  } finally {
    await a.context.close();
    await b.context.close();
  }
});

test("expired and throttled sessions stay visibly failed and never claim success", async ({ browser }) => {
  const a = await openAccount(browser, account({ userId: "user-expired-000000" }));
  const b = await openAccount(
    browser,
    account({ userId: "user-throttled-00000", meetingId: "m-b", title: "B 的会议", transcriptText: "B 的私有文字稿" }),
  );

  try {
    const expired = await a.page.evaluate(async (options) => {
      const [{ ChunkBuffer }, { RecordingSession }, { UploadPump }, { CloudRequestError, isSessionExpired }, { renderWorkbench }] =
        await Promise.all([
          import("/web/cloud/buffer.mjs"),
          import("/web/cloud/recording.mjs"),
          import("/web/cloud/upload.mjs"),
          import("/web/cloud/session.mjs"),
          import("/web/cloud/workbench.mjs"),
        ]);
      const buffer = await ChunkBuffer.open({ name: "cloud-recorder-expiry" });
      await buffer.clearOwner(options.userId);
      const transport = {
        putChunk: async () => {
          throw new CloudRequestError(401, "session_expired", { error: "session_expired" });
        },
        seal: async () => ({ status: "complete", input_version: 1, gaps: [] }),
      };
      const pump = new UploadPump({
        buffer,
        transport,
        maxAttempts: 1,
        backoffMs: [1],
        sleep: async () => {},
      });
      let tick = 0;
      const session = new RecordingSession({
        userId: options.userId,
        uploadId: options.uploadId,
        mode: "mic",
        mediaDevices: window.__fakeMediaDevices,
        buffer,
        pump,
        clock: { now: () => (tick += 250) },
        chunkTargetMs: 25,
        captureOptions: { RecorderCtor: window.__FakeCaptureRecorder },
      });
      await session.start();
      for (let index = 0; index < 150; index += 1) {
        const snapshot = await session.snapshot();
        if (snapshot.upload.error) break;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      const failedFlush = await pump.flush(options.userId, options.uploadId);
      const expired = isSessionExpired(
        new CloudRequestError(401, "session_expired", { error: "session_expired" }),
      );
      await session.handleSessionExpired();
      const snapshot = await session.snapshot();
      const stats = await buffer.stats(options.userId, options.uploadId);
      const root = document.createElement("div");
      document.body.append(root);
      renderWorkbench(root, {
        loading: false,
        error: null,
        loginRequired: false,
        session: { user_id: options.userId },
        history: [],
        meeting: null,
        transcripts: [],
        annotations: [],
        recording: snapshot,
      });
      const warning = root.querySelector('[data-testid="upload-warning"]');
      const uploadError = root.querySelector('[data-testid="upload-error"]');
      const captureState = root.querySelector('[data-testid="capture-state"]');
      const result = {
        snapshot,
        stats,
        expired,
        flushOk: failedFlush.ok,
        warningHidden: warning.hidden,
        warningText: warning.textContent,
        errorHidden: uploadError.hidden,
        errorText: uploadError.textContent,
        captureText: captureState.textContent,
        innerText: root.innerText,
      };
      buffer.close?.();
      return result;
    }, { userId: "user-expired-000000", uploadId: "upload-expired" });

    expect(expired.expired).toBe(true);
    expect(expired.flushOk).toBe(false);
    expect(expired.snapshot.captureRunning).toBe(false);
    expect(expired.snapshot.capture.state).toBe("interrupted");
    expect(expired.snapshot.capture.reason).toBe("session_expired");
    expect(expired.snapshot.upload.state).toBe("buffered_offline");
    expect(expired.stats.chunks).toBeGreaterThan(0);
    expect(expired.warningHidden).toBe(false);
    expect(expired.warningText).toContain("Sign in again to resume uploading buffered audio");
    expect(expired.errorHidden).toBe(false);
    expect(expired.errorText).toContain("401");
    expect(expired.errorText).toContain("session_expired");
    expect(expired.captureText).toContain("采集中断");
    expect(expired.innerText).not.toContain("上传成功");
    expect(expired.innerText).not.toContain("保存成功");
    expect(expired.innerText).not.toContain("正在录音");

    const throttled = await b.page.evaluate(async (options) => {
      const [{ ChunkBuffer }, { RecordingSession }, { UploadPump }, { CloudRequestError }, { renderWorkbench }] =
        await Promise.all([
          import("/web/cloud/buffer.mjs"),
          import("/web/cloud/recording.mjs"),
          import("/web/cloud/upload.mjs"),
          import("/web/cloud/session.mjs"),
          import("/web/cloud/workbench.mjs"),
        ]);
      const buffer = await ChunkBuffer.open({ name: "cloud-recorder-throttle" });
      await buffer.clearOwner(options.userId);
      const transport = {
        putChunk: async () => {
          throw new CloudRequestError(429, "rate_limited", {
            error: "rate_limited",
            retry_after: 7,
          });
        },
        seal: async () => ({ status: "complete", input_version: 1, gaps: [] }),
      };
      const pump = new UploadPump({
        buffer,
        transport,
        maxAttempts: 1,
        backoffMs: [1],
        sleep: async () => {},
      });
      let tick = 0;
      const session = new RecordingSession({
        userId: options.userId,
        uploadId: options.uploadId,
        mode: "mic",
        mediaDevices: window.__fakeMediaDevices,
        buffer,
        pump,
        clock: { now: () => (tick += 250) },
        chunkTargetMs: 25,
        captureOptions: { RecorderCtor: window.__FakeCaptureRecorder },
      });
      await session.start();
      for (let index = 0; index < 150; index += 1) {
        const snapshot = await session.snapshot();
        if (snapshot.upload.error) break;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      const snapshot = await session.snapshot();
      const stats = await buffer.stats(options.userId, options.uploadId);
      const root = document.createElement("div");
      document.body.append(root);
      renderWorkbench(root, {
        loading: false,
        error: null,
        loginRequired: false,
        session: { user_id: options.userId },
        history: [],
        meeting: null,
        transcripts: [],
        annotations: [],
        recording: snapshot,
      });
      const warning = root.querySelector('[data-testid="upload-warning"]');
      const uploadError = root.querySelector('[data-testid="upload-error"]');
      const result = {
        snapshot,
        stats,
        warningHidden: warning.hidden,
        errorHidden: uploadError.hidden,
        errorText: uploadError.textContent,
        innerText: root.innerText,
      };
      await session.stop();
      buffer.close?.();
      return result;
    }, { userId: "user-throttled-00000", uploadId: "upload-throttled" });

    expect(throttled.snapshot.captureRunning).toBe(true);
    expect(throttled.snapshot.upload.state).toBe("buffered_offline");
    expect(throttled.snapshot.upload.error).toContain("429");
    expect(throttled.snapshot.upload.error).toContain("rate_limited");
    expect(throttled.stats.chunks).toBeGreaterThan(0);
    expect(throttled.warningHidden).toBe(true);
    expect(throttled.errorHidden).toBe(false);
    expect(throttled.errorText).toContain("429");
    expect(throttled.errorText).toContain("rate_limited");
    expect(throttled.innerText).not.toContain("上传成功");
    expect(throttled.innerText).not.toContain("保存成功");
    expect(throttled.innerText).not.toContain("录音已保存");
  } finally {
    await a.context.close();
    await b.context.close();
  }
});
