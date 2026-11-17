// Bounded upload pump for buffered chunks.
//
// Reads a small batch at a time, sends one chunk per request, deletes the chunk
// only after the server acknowledges it, and retries with capped exponential
// backoff. A failed flush leaves every unacknowledged chunk in IndexedDB.

export const DEFAULT_BACKOFF_MS = [250, 500, 1000, 2000, 4000];
export const MAX_ATTEMPTS_PER_FLUSH = 5;
export const DEFAULT_MAX_BATCH = 4;

export class UploadError extends Error {
  constructor(message, body = null) {
    super(message);
    this.name = "UploadError";
    this.body = body;
  }
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class UploadPump {
  constructor(options = {}) {
    const {
      buffer,
      transport,
      sleep = defaultSleep,
      maxAttempts = MAX_ATTEMPTS_PER_FLUSH,
      backoffMs = DEFAULT_BACKOFF_MS,
      maxBatch = DEFAULT_MAX_BATCH,
      onState = () => {},
    } = options;
    if (!buffer) throw new UploadError("buffer is required");
    if (!transport || typeof transport.putChunk !== "function") {
      throw new UploadError("transport is required");
    }
    this.buffer = buffer;
    this.transport = transport;
    this.sleep = sleep;
    this.maxAttempts = Math.max(1, maxAttempts);
    this.backoffMs = backoffMs.length ? backoffMs : DEFAULT_BACKOFF_MS;
    this.maxBatch = Math.max(1, maxBatch);
    this.onState = onState;
    this.lastAck = null;
    this.metrics = {
      flushes: 0,
      attempts: 0,
      acked: 0,
      peakBatch: 0,
      lastError: null,
    };
    this._inflight = new Map();
  }

  flush(userId, uploadId) {
    const key = `${userId}\u0000${uploadId}`;
    const existing = this._inflight.get(key);
    if (existing) return existing;
    const run = this._flush(userId, uploadId).finally(() => {
      this._inflight.delete(key);
    });
    this._inflight.set(key, run);
    return run;
  }

  async _flush(userId, uploadId) {
    this.metrics.flushes += 1;
    let acked = 0;
    let attempts = 0;
    let consecutiveFailures = 0;
    let lastError = null;

    for (;;) {
      const batch = await this.buffer.pending(userId, uploadId, { limit: this.maxBatch });
      this.metrics.peakBatch = Math.max(this.metrics.peakBatch, batch.length);
      if (batch.length === 0) break;

      try {
        for (const entry of batch) {
          attempts += 1;
          this.metrics.attempts += 1;
          this.onState({ state: "sending", sequence: entry.meta.sequence, trackId: entry.meta.trackId });
          const response = await this.transport.putChunk({
            meta: entry.meta,
            bytes: entry.bytes,
          });
          if (!response || response.accepted !== true) {
            throw new UploadError("server_rejected", response);
          }
          // Deletion happens only after a real ACK; a throw above skips it.
          await this.buffer.ack(userId, entry.meta, response);
          acked += 1;
          this.metrics.acked += 1;
          this.lastAck = {
            sequence: entry.meta.sequence,
            trackId: entry.meta.trackId,
            captureEpochId: entry.meta.captureEpochId,
            endMs: entry.meta.endMs,
            duplicate: Boolean(response.duplicate),
            contiguousMs: Number.isFinite(response.contiguous_ms) ? response.contiguous_ms : null,
          };
        }
        consecutiveFailures = 0;
      } catch (error) {
        consecutiveFailures += 1;
        lastError = error;
        const retryAfter = Number(error?.retry_after ?? error?.body?.retry_after);
        this.metrics.lastError = {
          message: error?.message ?? String(error),
          name: error?.name ?? "UploadError",
          code: typeof error?.code === "string" ? error.code : null,
          status: Number.isInteger(error?.status) ? error.status : null,
          retryAfter: Number.isFinite(retryAfter) ? retryAfter : null,
        };
        if (consecutiveFailures >= this.maxAttempts) {
          const remaining = await this.buffer.pending(userId, uploadId);
          this.onState({ state: "failed", pending: remaining.length });
          return {
            ok: false,
            acked,
            attempts,
            pending: remaining.length,
            error: this.metrics.lastError,
          };
        }
        const index = Math.min(consecutiveFailures - 1, this.backoffMs.length - 1);
        this.onState({ state: "backoff", delayMs: this.backoffMs[index] });
        await this.sleep(this.backoffMs[index]);
      }
    }

    this.onState({ state: "idle", pending: 0 });
    return { ok: true, acked, attempts, pending: 0, lastAck: this.lastAck };
  }
}

// The server stores integer milliseconds (TrackManifest.end_ms is an int), while
// the recorder clock is fractional. Round outward, matching putChunk's ranges.
function wireManifest(manifest) {
  if (!manifest || !Array.isArray(manifest.tracks)) return manifest;
  return {
    ...manifest,
    tracks: manifest.tracks.map((track) => ({
      ...track,
      end_ms: Number.isFinite(track.end_ms) ? Math.max(0, Math.ceil(track.end_ms)) : track.end_ms,
    })),
  };
}

export function createHttpTransport({ session, baseUrl = "" } = {}) {
  if (!session || typeof session.request !== "function") {
    throw new UploadError("cloud session is required");
  }
  // The server stores integer millisecond ranges and rejects end_ms <= start_ms.
  // Local chunk metadata keeps the recorder's exact clock value (and the plan's
  // epoch boundary at 30000); only the wire representation is rounded outward.
  // A cursor per track keeps expanded zero-length chunks from overlapping the
  // next chunk when the recorder emits two events at the same clock reading.
  const cursors = new Map();
  const rangeFor = (meta) => {
    const key = [meta.uploadSessionId, meta.captureEpochId, meta.trackId].join("\u0000");
    let start = Math.max(0, Math.floor(meta.startMs));
    let end = Math.max(Math.ceil(meta.endMs), start + 1);
    const previous = cursors.get(key);
    if (previous && previous.sequence === meta.sequence) return previous.range;
    if (previous && meta.sequence > previous.sequence && start < previous.range.end) {
      start = previous.range.end;
      end = Math.max(end, start + 1);
    }
    const range = { start, end };
    cursors.set(key, { sequence: meta.sequence, range });
    return range;
  };
  return {
    async putChunk({ meta, bytes }) {
      const range = rangeFor(meta);
      const query = new URLSearchParams({
        start_ms: String(range.start),
        end_ms: String(range.end),
        checksum: meta.checksum,
        codec: meta.codec ?? "application/octet-stream",
      });
      const path =
        `/api/cloud/uploads/${encodeURIComponent(meta.uploadSessionId)}` +
        `/chunks/${encodeURIComponent(meta.captureEpochId)}` +
        `/${encodeURIComponent(meta.trackId)}/${encodeURIComponent(meta.sequence)}` +
        `?${query.toString()}`;
      return session.request(baseUrl ? new URL(path, baseUrl).toString() : path, {
        method: "PUT",
        headers: { "Content-Type": meta.codec ?? "application/octet-stream" },
        body: bytes,
      });
    },

    async seal(uploadId, manifest) {
      const path = `/api/cloud/uploads/${encodeURIComponent(uploadId)}/seal`;
      return session.request(baseUrl ? new URL(path, baseUrl).toString() : path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: wireManifest(manifest),
      });
    },
  };
}
