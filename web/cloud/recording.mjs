// Recording session: capture -> durable buffer -> acknowledged upload.
//
// Capture, upload, and processing are reported separately. A refresh or an
// expired session never claims capture is still running; it reports the durable
// same-owner buffer that can be resumed after re-authentication.

import { CHUNK_TARGET_MS, startCapture } from "./capture.mjs";
import { QUOTA_LIMITS, evaluateQuota } from "./buffer.mjs";

const LOGGED_OUT = "logged_out";
const ACTIVE = "active";

// A local downgrade is evidence the server may not have: when the system track
// was never registered, the server can only answer "complete" for the mic
// manifest. Keep the locally known gap and never lose it to a server verb.

const TASK_STATES = Object.freeze({
  waiting: "queued",
  running: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
});

// Server events describe processing only. They never assert that capture or
// upload is running, and no transcript text or speaker label is invented here.
function processingFromTask(task) {
  if (!task || typeof task !== "object") return null;
  const state = TASK_STATES[task.status] ?? "unknown";
  const result = { state };
  if (typeof task.id === "string") result.taskId = task.id;
  if (typeof task.task_id === "string") result.taskId = task.task_id;
  if (Number.isInteger(task.input_version)) result.inputVersion = task.input_version;
  if (Number.isInteger(task.generation)) result.generation = task.generation;
  if (typeof task.error_code === "string") result.errorCode = task.error_code;
  return result;
}

function processingFromSnapshot(data) {
  const tasks = Array.isArray(data?.tasks)
    ? data.tasks.filter((task) => task && typeof task === "object")
    : [];
  const active = tasks.find(
    (task) => task.status === "running" || task.status === "waiting",
  );
  if (active) return processingFromTask(active);
  if (data?.transcript && typeof data.transcript.id === "string") {
    return { state: "completed", transcriptId: data.transcript.id };
  }
  if (tasks.length > 0) return processingFromTask(tasks[0]);
  return { state: "not_submitted" };
}

function mergeSealGaps(...groups) {
  const merged = new Map();
  for (const group of groups) {
    for (const gap of group ?? []) {
      if (!gap || typeof gap.track_id !== "string") continue;
      merged.set(
        `${gap.track_id}|${gap.start_ms}|${gap.end_ms}|${gap.reason}`,
        { ...gap },
      );
    }
  }
  return [...merged.values()];
}

export class RecordingSession {
  constructor(options = {}) {
    const {
      userId,
      uploadId,
      mode = "mic",
      epoch = {},
      mediaDevices = null,
      buffer,
      pump,
      clock = { now: () => globalThis.performance?.now?.() ?? Date.now() },
      onState = () => {},
      closeEvents = null,
      quota = QUOTA_LIMITS,
      chunkTargetMs = CHUNK_TARGET_MS,
      captureOptions = {},
    } = options;

    if (typeof userId !== "string" || userId.length === 0) {
      throw new TypeError("userId is required");
    }
    if (typeof uploadId !== "string" || uploadId.length === 0) {
      throw new TypeError("uploadId is required");
    }
    if (!buffer || typeof buffer.put !== "function") throw new TypeError("buffer is required");
    if (!pump || typeof pump.flush !== "function") throw new TypeError("pump is required");

    this.userId = userId;
    this.uploadId = uploadId;
    this.mode = mode;
    this.epoch = {
      id: epoch.id ?? "epoch",
      offsetMs: Number.isFinite(epoch.offsetMs) ? epoch.offsetMs : 0,
      anchorMs: Number.isFinite(epoch.anchorMs) ? epoch.anchorMs : clock.now(),
    };
    this.mediaDevices = mediaDevices;
    this.buffer = buffer;
    this.pump = pump;
    this.clock = clock;
    this.onState = onState;
    this.closeEvents = closeEvents;
    this.quota = quota;
    this.chunkTargetMs = chunkTargetMs;
    this.captureOptions = captureOptions;

    this.capture = { state: "idle", error: null, reason: null, startedAt: null, stoppedAt: null };
    this.upload = {
      state: "idle",
      pendingChunks: 0,
      pendingBytes: 0,
      bufferedMs: 0,
      ackedChunks: 0,
      lastAck: null,
      warning: null,
      error: null,
      errorCode: null,
      errorStatus: null,
    };
    this.processing = { state: "not_submitted" };
    this.handle = null;
    this.stopPromise = null;
    this.logoutState = ACTIVE;
    this.quotaStopped = false;
    this.storageFailed = false;
    this.eventsClosed = false;
    this.seen = new Map();
    this.seal = null;
    this.sealError = null;
    this.downgrade = null;
    this.eventStream = null;
    this.eventCursor = 0;
  }

  static async restore(options = {}) {
    const session = new RecordingSession({
      ...options,
      epoch: options.epoch ?? { id: "restored", offsetMs: 0, anchorMs: 0 },
    });
    session.capture = {
      state: "interrupted",
      error: null,
      reason: "page_reload",
      startedAt: null,
      stoppedAt: null,
    };
    await session.refreshUploadStats();
    try {
      session.upload.warning = await session.buffer.getWarning(session.userId, session.uploadId);
    } catch {
      session.upload.warning = null;
    }
    session.upload.state = session.upload.pendingChunks > 0 ? "buffered_offline" : "idle";
    return session;
  }

  async start() {
    if (this.handle) throw new Error("capture already started");
    try {
      const warning = await this.buffer.getWarning(this.userId, this.uploadId);
      if (warning) this.upload.warning = warning;
    } catch {
      // A warning read failure must not prevent recording.
    }
    const handle = await startCapture({
      mode: this.mode,
      epoch: this.epoch,
      mediaDevices: this.mediaDevices,
      clock: this.clock,
      chunkTargetMs: this.chunkTargetMs,
      onChunk: (chunk) => this.handleChunk(chunk),
      onState: (event) => this.handleCaptureState(event),
      ...this.captureOptions,
    });
    this.handle = handle;
    await this.refreshUploadStats();
    return this.snapshot();
  }

  async stop({ confirmDowngrade = false } = {}) {
    if (this.stopPromise) return this.stopPromise;
    this.stopPromise = (async () => {
      // handle.stop() awaits both recorders' tail chunks and the chunk chain,
      // so every byte captured before the user clicked stop is in IndexedDB
      // before the manifest is built.
      if (this.handle) await this.handle.stop();
      await this.refreshUploadStats();
      if (this.upload.pendingChunks > 0 && !this.storageFailed) {
        await this.drain();
      }
      if (this.capture.state !== "storage_failed" && this.capture.state !== "interrupted") {
        this.capture.state = "stopped";
      }
      this.capture.stoppedAt = this.clock.now();
      const sealed = await this.sealRecording({ confirmDowngrade });
      if (sealed?.status === "downgrade_confirmation_required") {
        // Releasing stop() lets the caller ask the user and call stop again
        // with explicit confirmation; nothing is sealed in the meantime.
        this.stopPromise = null;
        return {
          ...(await this.snapshot()),
          status: "downgrade_confirmation_required",
          seal: null,
          downgrade: sealed.downgrade,
        };
      }
      return this.snapshot();
    })();
    return this.stopPromise;
  }

  async requestSystem() {
    if (!this.handle || typeof this.handle.requestSystem !== "function") {
      throw new Error("system capture is unavailable");
    }
    const result = await this.handle.requestSystem();
    if (this.capture.shareError) this.capture.shareError = null;
    await this.refreshUploadStats();
    return result ?? this.snapshot();
  }

  buildManifest() {
    const entries = new Map();
    for (const record of this.seen.values()) {
      entries.set(`${record.captureEpochId}:${record.trackId}`, {
        capture_epoch_id: record.captureEpochId,
        track_id: record.trackId,
        last_sequence: record.lastSequence,
        end_ms: record.endMs,
        source: record.source,
      });
    }
    // A registered track that failed before producing any chunk must still be
    // in the manifest: omitting it would hide a missing system recording.
    const offset = this.epoch.offsetMs;
    for (const spec of this.handle?.specs?.() ?? []) {
      const key = `${this.epoch.id}:${spec.id}`;
      if (!entries.has(key)) {
        entries.set(key, {
          capture_epoch_id: this.epoch.id,
          track_id: spec.id,
          last_sequence: -1,
          end_ms: offset,
          source: spec.source,
        });
      }
    }
    return [...entries.values()].sort(
      (left, right) =>
        left.capture_epoch_id.localeCompare(right.capture_epoch_id) ||
        left.track_id.localeCompare(right.track_id),
    );
  }

  async sealRecording({ confirmDowngrade = false } = {}) {
    if (this.storageFailed || this.logoutState === LOGGED_OUT) return null;
    const transport = this.pump?.transport;
    if (!transport || typeof transport.seal !== "function") return null;
    const manifest = this.buildManifest();
    if (manifest.length === 0) return null;

    const overallEnd = Math.max(...manifest.map((entry) => entry.end_ms), this.epoch.offsetMs);
    const systemEntries = manifest.filter(
      (entry) => entry.source === "system" || entry.track_id.startsWith("system"),
    );
    const systemEnd = systemEntries.length
      ? Math.max(...systemEntries.map((entry) => entry.end_ms))
      : null;
    const systemHasChunks = systemEntries.some((entry) => entry.last_sequence >= 0);
    const downgraded = this.mode === "dual" && overallEnd > 0 && (systemEnd === null || systemEnd < overallEnd);
    let gap = null;
    if (downgraded) {
      gap = {
        track_id: "system",
        start_ms: systemEnd ?? 0,
        end_ms: overallEnd,
        reason: systemHasChunks ? "missing_tail" : "missing_track",
      };
      if (!confirmDowngrade) {
        return {
          status: "downgrade_confirmation_required",
          downgrade: {
            from: "dual",
            to: "mic",
            changed_at_ms: this.clock.now(),
            gap,
          },
        };
      }
      this.downgrade = {
        from: "dual",
        to: "mic",
        changed_at_ms: this.clock.now(),
        gap,
      };
    }

    const tracks = manifest.map(({ source, ...entry }) => entry);
    try {
      const result = await transport.seal(this.uploadId, {
        tracks,
        accept_partial: Boolean(gap) || Boolean(this.downgrade),
      });
      const localGap = this.downgrade?.gap ?? gap ?? null;
      const downgradedSeal = Boolean(this.downgrade) || Boolean(gap);
      const serverGaps = Array.isArray(result?.gaps)
        ? result.gaps.map((item) => ({ ...item }))
        : [];
      this.seal = {
        // The client's confirmed downgrade outranks a server that never knew a
        // second track existed: a dual capture that lost system audio is
        // partial, with the local gap preserved, even if the server says
        // complete for the microphone-only manifest.
        status: downgradedSeal ? "partial" : result?.status === "complete" ? "complete" : "partial",
        input_version: Number.isFinite(result?.input_version) ? result.input_version : 0,
        gaps: mergeSealGaps(serverGaps, localGap ? [localGap] : []),
      };
      this.sealError = null;
      this.setUploadState(this.seal.status === "complete" ? "complete" : "partial");
      if (this.seal.gaps.length > 0) {
        this.upload.warning = {
          code: "recording_gap",
          message: "Recording sealed with visible gaps",
          at: Date.now(),
          gaps: this.seal.gaps,
        };
      }
      this.emit({ state: "sealed", status: this.seal.status, gaps: this.seal.gaps });
    } catch (error) {
      this.sealError = {
        code: error?.code ?? error?.name ?? "seal_failed",
        message: error?.message ?? String(error),
      };
      this.emit({ state: "seal_failed", ...this.sealError });
    }
    return this.seal;
  }

  async resume() {
    await this.refreshUploadStats();
    if (this.upload.pendingChunks > 0) {
      await this.drain();
    }
    await this.refreshUploadStats();
    return this.snapshot();
  }

  async handleSessionExpired() {
    if (this.logoutState === LOGGED_OUT) return this.snapshot();
    if (this.handle) await this.handle.stop();
    await this.refreshUploadStats();
    this.capture.state = "interrupted";
    this.capture.error = null;
    this.capture.reason = "session_expired";
    this.upload.state = "buffered_offline";
    const warning = {
      code: "session_expired",
      message: "Sign in again to resume uploading buffered audio",
      at: Date.now(),
      pendingChunks: this.upload.pendingChunks,
    };
    this.upload.warning = warning;
    try {
      await this.buffer.putWarning(this.userId, this.uploadId, warning);
    } catch {
      // The in-memory warning is still truthful for this page.
    }
    this.closeEventStream();
    this.emit({ state: "session_expired", pendingChunks: this.upload.pendingChunks });
    return this.snapshot();
  }

  async logout({ abandon = false } = {}) {
    if (this.logoutState === LOGGED_OUT) {
      return { status: "logged_out", pendingChunks: 0 };
    }
    if (this.handle) await this.handle.stop();
    await this.refreshUploadStats();

    if (this.upload.pendingChunks > 0 && !abandon) {
      this.upload.state = "awaiting_confirmation";
      return {
        status: "confirmation_required",
        pendingChunks: this.upload.pendingChunks,
        pendingBytes: this.upload.pendingBytes,
        bufferedMs: this.upload.bufferedMs,
      };
    }

    if (this.upload.pendingChunks > 0) {
      await this.buffer.clearUpload(this.userId, this.uploadId);
    } else {
      try {
        await this.buffer.clearWarning(this.userId, this.uploadId);
      } catch {
        // Nothing to clear.
      }
    }

    this.closeEventStream();
    this.upload = {
      state: "abandoned",
      pendingChunks: 0,
      pendingBytes: 0,
      bufferedMs: 0,
      ackedChunks: 0,
      lastAck: null,
      warning: null,
      error: null,
      errorCode: null,
      errorStatus: null,
    };
    this.capture.state = "stopped";
    this.capture.stoppedAt = this.clock.now();
    this.logoutState = LOGGED_OUT;
    this.handle = null;
    return { status: "logged_out", pendingChunks: 0 };
  }

  async snapshot() {
    const stats = await this.safeStats();
    let warning = this.upload.warning;
    if (!warning) {
      try {
        warning = await this.buffer.getWarning(this.userId, this.uploadId);
      } catch {
        warning = null;
      }
    }
    const tracks = this.handle?.specs?.() ?? [];
    const running = this.capture.state === "recording" && !this.quotaStopped && !this.storageFailed;
    return {
      userId: this.userId,
      uploadId: this.uploadId,
      mode: this.mode,
      epochId: this.epoch.id,
      captureRunning: running,
      capture: { ...this.capture },
      upload: {
        ...this.upload,
        pendingChunks: stats.chunks,
        pendingBytes: stats.bytes,
        bufferedMs: stats.bufferedMs,
        warning,
      },
      processing: { ...this.processing },
      tracks,
      seal: this.seal ? { ...this.seal, gaps: this.seal.gaps.map((gap) => ({ ...gap })) } : null,
      sealError: this.sealError ? { ...this.sealError } : null,
      downgrade: this.downgrade ? { ...this.downgrade } : null,
      warnings: warning ? [warning] : [],
    };
  }

  async handleChunk({ meta, bytes, trackSpec }) {
    if (this.logoutState === LOGGED_OUT) throw new Error("session_logged_out");
    const size = bytes?.size ?? bytes?.byteLength ?? 0;
    let checksum;
    try {
      checksum = await sha256Hex(bytes);
      const fullMeta = {
        uploadSessionId: this.uploadId,
        captureEpochId: this.epoch.id,
        epochOffsetMs: this.epoch.offsetMs,
        trackId: meta.trackId,
        source: meta.source ?? trackSpec?.source ?? null,
        sequence: meta.sequence,
        startMs: meta.startMs,
        endMs: meta.endMs,
        checksum,
        codec: meta.codec ?? trackSpec?.codec ?? "audio/webm",
        sampleRate: meta.sampleRate ?? trackSpec?.sample_rate ?? null,
        byteLength: size,
        timesliceMs: meta.timesliceMs ?? this.chunkTargetMs,
      };
      await this.buffer.put(this.userId, fullMeta, bytes);
      const key = `${fullMeta.captureEpochId}:${fullMeta.trackId}`;
      const prior = this.seen.get(key);
      this.seen.set(key, {
        captureEpochId: fullMeta.captureEpochId,
        trackId: fullMeta.trackId,
        source: fullMeta.source,
        lastSequence:
          prior === undefined ? fullMeta.sequence : Math.max(prior.lastSequence, fullMeta.sequence),
        endMs: prior === undefined ? fullMeta.endMs : Math.max(prior.endMs, fullMeta.endMs),
      });
    } catch (error) {
      await this.storageFailure(error);
      throw error;
    }

    await this.refreshUploadStats();
    if (!this.quotaStopped && !this.storageFailed) {
      this.setUploadState("pending");
    }
    await this.applyQuota();
    await this.drain();
  }

  async handleCaptureState(event) {
    switch (event.state) {
      case "requesting_permissions":
        this.capture.state = "requesting_permissions";
        break;
      case "recording":
        this.capture.state = "recording";
        this.capture.error = null;
        this.capture.reason = null;
        this.capture.shareError = event.shareError ?? null;
        this.capture.startedAt = this.clock.now();
        break;
      case "system_joined":
        this.capture.shareError = null;
        break;
      case "system_unavailable":
        this.capture.shareError = {
          error: event.error ?? "system_unavailable",
          reason: event.reason ?? null,
        };
        break;
      case "permission_denied":
        this.capture.state = "permission_denied";
        this.capture.error = event.error ?? "permission_denied";
        this.capture.reason = event.reason ?? null;
        break;
      case "capture_failed":
      case "recorder_error":
        this.capture.state = "capture_failed";
        this.capture.error = event.error ?? "capture_failed";
        this.capture.reason = event.reason ?? null;
        break;
      case "stopping":
        if (!this.storageFailed && this.capture.state !== "interrupted") {
          this.capture.state = "stopping";
        }
        break;
      case "stopped":
        this.capture.stoppedAt = this.clock.now();
        if (!this.storageFailed && this.capture.state !== "interrupted") {
          this.capture.state = "stopped";
        }
        break;
      default:
        break;
    }
    this.emit(event);
  }

  async drain() {
    if (this.storageFailed) return;
    const result = await this.pump.flush(this.userId, this.uploadId);
    this.upload.ackedChunks += Number(result?.acked) || 0;
    if (this.pump.lastAck) this.upload.lastAck = { ...this.pump.lastAck };
    await this.refreshUploadStats();
    if (!result?.ok) {
      const failure = result?.error ?? null;
      this.upload.error = failure?.message ?? "upload_failed";
      this.upload.errorCode = typeof failure?.code === "string" ? failure.code : null;
      this.upload.errorStatus = Number.isInteger(failure?.status) ? failure.status : null;
      this.setUploadState("buffered_offline");
      return;
    }
    this.upload.error = null;
    this.upload.errorCode = null;
    this.upload.errorStatus = null;
    if (this.upload.pendingChunks > 0) {
      this.setUploadState(this.capture.state === "recording" ? "pending" : "buffered_offline");
      return;
    }
    this.setUploadState("complete");
    if (this.upload.warning?.code === "session_expired") {
      this.upload.warning = null;
      try {
        await this.buffer.clearWarning(this.userId, this.uploadId);
      } catch {
        // Warning cleanup is best effort; the upload itself succeeded.
      }
    }
  }

  async applyQuota() {
    if (this.quotaStopped || this.storageFailed) return this.quotaStopped;
    const stats = await this.safeStats();
    const verdict = evaluateQuota(
      { bufferedMs: stats.bufferedMs, bufferedBytes: stats.bytes },
      this.quota,
    );
    if (!verdict.exceeded) return false;

    this.quotaStopped = true;
    const warning = {
      code: verdict.code,
      message:
        verdict.code === "quota_ms"
          ? "Buffered recording reached the 5 minute limit; capture stopped"
          : "Buffered recording reached the 100 MiB limit; capture stopped",
      at: Date.now(),
      bufferedMs: verdict.bufferedMs,
      bufferedBytes: verdict.bufferedBytes,
    };
    this.upload.warning = warning;
    try {
      await this.buffer.putWarning(this.userId, this.uploadId, warning);
    } catch {
      // In-memory warning remains visible even if the store is full.
    }
    this.setUploadState("quota_stopped");
    this.emit({ state: "quota_stopped", code: verdict.code });
    const handle = this.handle;
    if (handle) {
      // Detached on purpose: this runs inside capture's onChunk chain.
      void handle.stop().catch(() => {});
    }
    return true;
  }

  async storageFailure(error) {
    if (this.storageFailed) return;
    this.storageFailed = true;
    this.capture.state = "storage_failed";
    this.capture.error = "storage_failed";
    this.capture.reason = error?.message ?? String(error);
    const warning = {
      code: "storage_failure",
      message: "Browser storage failed; recording stopped to avoid losing audio",
      at: Date.now(),
      detail: error?.message ?? String(error),
    };
    this.upload.warning = warning;
    this.upload.state = "storage_failed";
    this.upload.error = warning.message;
    try {
      await this.buffer.putWarning(this.userId, this.uploadId, warning);
    } catch {
      // The in-memory warning is the fallback.
    }
    this.emit({ state: "storage_failed", error: "storage_failed" });
    const handle = this.handle;
    if (handle) {
      void handle.stop().catch(() => {});
    }
  }

  setUploadState(state) {
    if (this.storageFailed) {
      this.upload.state = "storage_failed";
      return;
    }
    if (this.quotaStopped) {
      this.upload.state = "quota_stopped";
      return;
    }
    this.upload.state = state;
  }

  async refreshUploadStats() {
    const stats = await this.safeStats();
    this.upload.pendingChunks = stats.chunks;
    this.upload.pendingBytes = stats.bytes;
    this.upload.bufferedMs = stats.bufferedMs;
    return stats;
  }

  async safeStats() {
    try {
      return await this.buffer.stats(this.userId, this.uploadId);
    } catch {
      return { chunks: 0, bytes: 0, bufferedMs: 0, byTrack: {} };
    }
  }

  attachEventStream(stream) {
    if (!stream || typeof stream.close !== "function") return null;
    if (this.eventsClosed) {
      try {
        stream.close();
      } catch {
        // A late stream is closed immediately after logout/expiry.
      }
      return null;
    }
    if (this.eventStream && this.eventStream !== stream) {
      try {
        this.eventStream.close?.();
      } catch {
        // Replacing a stream is best effort.
      }
    }
    this.eventStream = stream;
    return stream;
  }

  applyServerEvent(event) {
    const name = typeof event?.event === "string" ? event.event : null;
    if (!name) return { ...this.processing };
    const cursor = Number(event?.cursor);
    if (Number.isInteger(cursor) && cursor >= 0) {
      // EventSource guarantees order; dropping an older cursor keeps a delayed
      // replay from rolling visible processing state backwards.
      if (cursor < this.eventCursor) return { ...this.processing };
      this.eventCursor = cursor;
    }
    const data =
      event?.data && typeof event.data === "object" && !Array.isArray(event.data)
        ? event.data
        : {};
    let next = null;
    if (name === "snapshot") next = processingFromSnapshot(data);
    else if (name === "task.updated") next = processingFromTask(data);
    else if (name === "transcript.published") {
      next = { state: "completed" };
      if (typeof data.version_id === "string") next.transcriptId = data.version_id;
      if (typeof data.task_id === "string") next.taskId = data.task_id;
    }
    if (next) {
      this.processing = next;
      this.emit({ state: "processing", processing: { ...next } });
    }
    return { ...this.processing };
  }

  closeEventStream() {
    if (this.eventsClosed) return;
    this.eventsClosed = true;
    const stream = this.eventStream;
    this.eventStream = null;
    if (stream) {
      try {
        stream.close?.();
      } catch {
        // Closing the local event stream must never block logout.
      }
    }
    if (this.closeEvents && this.closeEvents !== stream) {
      try {
        this.closeEvents?.close?.();
      } catch {
        // A separate close hook is best effort too.
      }
    }
  }

  emit(event) {
    try {
      this.onState?.({
        ...event,
        capture: this.capture.state,
        upload: this.upload.state,
      });
    } catch {
      // UI listeners are not allowed to break recording.
    }
  }
}

async function sha256Hex(data) {
  const bytes =
    data instanceof Uint8Array
      ? data
      : new Uint8Array(await data.arrayBuffer());
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
