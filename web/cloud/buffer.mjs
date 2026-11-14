// Durable per-user chunk buffer backed by IndexedDB.
//
// The network layer is allowed to see a chunk only after it is committed here,
// and a chunk is removed only after the server has acknowledged it. Node 22
// compatible: no DOM-only APIs beyond optional Blob/ArrayBuffer handling.

export const MAX_BUFFERED_MS = 300000;
export const MAX_BUFFERED_BYTES = 100 * 1024 * 1024;
export const QUOTA_LIMITS = Object.freeze({
  maxBufferedMs: MAX_BUFFERED_MS,
  maxBufferedBytes: MAX_BUFFERED_BYTES,
});

export const CHUNK_STORE = "chunks";
export const WARNING_STORE = "warnings";
export const UPLOAD_INDEX = "by_upload";
export const DATABASE_VERSION = 1;

const KEY_PATH = ["ownerUserId", "uploadSessionId", "epochId", "trackId", "sequence"];
const WARNING_KEY_PATH = ["ownerUserId", "uploadSessionId"];
const HEX_CHECKSUM = /^[0-9a-f]{64}$/i;

export class StorageError extends Error {
  constructor(message, cause = null) {
    super(message);
    this.name = "StorageError";
    this.cause = cause;
  }
}

export function evaluateQuota(stats, limits = QUOTA_LIMITS) {
  const bufferedMs = Math.max(0, Number(stats?.bufferedMs) || 0);
  const bufferedBytes = Math.max(0, Number(stats?.bufferedBytes ?? stats?.bytes) || 0);
  if (bufferedMs >= limits.maxBufferedMs) {
    return {
      exceeded: true,
      code: "quota_ms",
      limit: limits.maxBufferedMs,
      bufferedMs,
      bufferedBytes,
    };
  }
  if (bufferedBytes >= limits.maxBufferedBytes) {
    return {
      exceeded: true,
      code: "quota_bytes",
      limit: limits.maxBufferedBytes,
      bufferedMs,
      bufferedBytes,
    };
  }
  return { exceeded: false, code: null, limit: null, bufferedMs, bufferedBytes };
}

export class ChunkBuffer {
  constructor(indexedDB, name, database) {
    this.indexedDB = indexedDB;
    this.name = name;
    this.database = database;
  }

  static async open({ indexedDB = globalThis.indexedDB, name = "cloud-recorder" } = {}) {
    if (!indexedDB || typeof indexedDB.open !== "function") {
      throw new StorageError("indexedDB is unavailable");
    }
    const database = await openDatabase(indexedDB, name);
    return new ChunkBuffer(indexedDB, name, database);
  }

  async put(userId, meta, bytes) {
    assertOwner(userId);
    assertMeta(meta);
    const payload = await toBytes(bytes);
    if (payload.byteLength === 0) {
      throw new StorageError("refusing to buffer an empty chunk");
    }
    const record = {
      ...meta,
      ownerUserId: userId,
      epochId: meta.captureEpochId,
      byteLength: payload.byteLength,
      bytes: payload,
      storedAt: Date.now(),
    };
    const tx = this.database.transaction([CHUNK_STORE], "readwrite");
    const done = transactionDone(tx);
    const request = tx.objectStore(CHUNK_STORE).put(record);
    try {
      await Promise.all([requestDone(request), done]);
    } catch (error) {
      throw asStorageError("chunk write failed", error);
    }
  }

  async pending(userId, uploadId, { limit = Infinity } = {}) {
    assertOwner(userId);
    const tx = this.database.transaction([CHUNK_STORE], "readonly");
    const done = transactionDone(tx);
    const request = tx.objectStore(CHUNK_STORE).index(UPLOAD_INDEX).getAll([userId, uploadId]);
    try {
      const records = await requestDone(request);
      await done;
      return records
        .sort(compareRecords)
        .slice(0, Math.max(0, limit))
        .map((record) => ({ meta: publicMeta(record), bytes: record.bytes }));
    } catch (error) {
      throw asStorageError("chunk read failed", error);
    }
  }

  async ack(userId, meta) {
    assertOwner(userId);
    assertMeta(meta);
    const key = [userId, meta.uploadSessionId, meta.captureEpochId, meta.trackId, meta.sequence];
    const tx = this.database.transaction([CHUNK_STORE], "readwrite");
    const done = transactionDone(tx);
    const request = tx.objectStore(CHUNK_STORE).delete(key);
    try {
      await Promise.all([requestDone(request), done]);
    } catch (error) {
      throw asStorageError("chunk acknowledgement delete failed", error);
    }
  }

  async stats(userId, uploadId) {
    const entries = await this.pending(userId, uploadId);
    return summarize(entries);
  }

  async putWarning(userId, uploadId, warning) {
    assertOwner(userId);
    if (!warning || typeof warning.code !== "string") {
      throw new StorageError("warning code is required");
    }
    const record = {
      ownerUserId: userId,
      uploadSessionId: uploadId,
      ...warning,
      at: warning.at ?? Date.now(),
    };
    const tx = this.database.transaction([WARNING_STORE], "readwrite");
    const done = transactionDone(tx);
    const request = tx.objectStore(WARNING_STORE).put(record);
    try {
      await Promise.all([requestDone(request), done]);
    } catch (error) {
      throw asStorageError("warning write failed", error);
    }
    return record;
  }

  async getWarning(userId, uploadId) {
    assertOwner(userId);
    const tx = this.database.transaction([WARNING_STORE], "readonly");
    const done = transactionDone(tx);
    const request = tx.objectStore(WARNING_STORE).get([userId, uploadId]);
    try {
      const record = await requestDone(request);
      await done;
      if (!record) return null;
      const { ownerUserId, uploadSessionId, ...warning } = record;
      return warning;
    } catch (error) {
      throw asStorageError("warning read failed", error);
    }
  }

  async clearWarning(userId, uploadId) {
    assertOwner(userId);
    const tx = this.database.transaction([WARNING_STORE], "readwrite");
    const done = transactionDone(tx);
    const request = tx.objectStore(WARNING_STORE).delete([userId, uploadId]);
    try {
      await Promise.all([requestDone(request), done]);
    } catch (error) {
      throw asStorageError("warning delete failed", error);
    }
  }

  async clearUpload(userId, uploadId) {
    assertOwner(userId);
    const tx = this.database.transaction([CHUNK_STORE, WARNING_STORE], "readwrite");
    const done = transactionDone(tx);
    const chunks = tx.objectStore(CHUNK_STORE);
    const warnings = tx.objectStore(WARNING_STORE);
    warnings.delete([userId, uploadId]);
    const request = chunks.index(UPLOAD_INDEX).getAll([userId, uploadId]);
    request.onsuccess = () => {
      for (const record of request.result) {
        chunks.delete([userId, record.uploadSessionId, record.epochId, record.trackId, record.sequence]);
      }
    };
    try {
      await done;
    } catch (error) {
      throw asStorageError("upload clear failed", error);
    }
  }

  async clearOwner(userId) {
    assertOwner(userId);
    const chunks = await this._allChunks();
    const tx = this.database.transaction([CHUNK_STORE, WARNING_STORE], "readwrite");
    const done = transactionDone(tx);
    const chunkStore = tx.objectStore(CHUNK_STORE);
    for (const record of chunks) {
      if (record.ownerUserId === userId) {
        chunkStore.delete([record.ownerUserId, record.uploadSessionId, record.epochId, record.trackId, record.sequence]);
      }
    }
    const request = tx.objectStore(WARNING_STORE).getAll();
    request.onsuccess = () => {
      for (const record of request.result) {
        if (record.ownerUserId === userId) {
          tx.objectStore(WARNING_STORE).delete([record.ownerUserId, record.uploadSessionId]);
        }
      }
    };
    try {
      await done;
    } catch (error) {
      throw asStorageError("owner clear failed", error);
    }
  }

  async _allChunks() {
    const tx = this.database.transaction([CHUNK_STORE], "readonly");
    const done = transactionDone(tx);
    const request = tx.objectStore(CHUNK_STORE).getAll();
    try {
      const records = await requestDone(request);
      await done;
      return records;
    } catch (error) {
      throw asStorageError("chunk scan failed", error);
    }
  }

  close() {
    this.database.close?.();
  }
}

function summarize(entries) {
  const perTrack = new Map();
  const perEpoch = new Map();
  let bytes = 0;
  for (const { meta, bytes: payload } of entries) {
    bytes += payload?.byteLength ?? meta.byteLength ?? 0;
    const trackKey = meta.trackId;
    const track = perTrack.get(trackKey) ?? { chunks: 0, bytes: 0, startMs: Infinity, endMs: 0 };
    track.chunks += 1;
    track.bytes += payload?.byteLength ?? meta.byteLength ?? 0;
    track.startMs = Math.min(track.startMs, meta.startMs);
    track.endMs = Math.max(track.endMs, meta.endMs);
    perTrack.set(trackKey, track);

    const epochKey = meta.captureEpochId;
    const epoch = perEpoch.get(epochKey) ?? new Map();
    const span = epoch.get(trackKey) ?? { startMs: Infinity, endMs: 0 };
    span.startMs = Math.min(span.startMs, meta.startMs);
    span.endMs = Math.max(span.endMs, meta.endMs);
    epoch.set(trackKey, span);
    perEpoch.set(epochKey, epoch);
  }

  let bufferedMs = 0;
  for (const epoch of perEpoch.values()) {
    let epochSpan = 0;
    for (const span of epoch.values()) {
      epochSpan = Math.max(epochSpan, span.endMs - span.startMs);
    }
    bufferedMs += epochSpan;
  }

  const byTrack = {};
  for (const [trackId, track] of perTrack) {
    byTrack[trackId] = {
      chunks: track.chunks,
      bytes: track.bytes,
      startMs: Number.isFinite(track.startMs) ? track.startMs : 0,
      endMs: track.endMs,
    };
  }
  return { chunks: entries.length, bytes, bufferedMs, byTrack };
}

function publicMeta(record) {
  const { ownerUserId, epochId, storedAt, bytes, ...meta } = record;
  return meta;
}

function compareRecords(left, right) {
  const epoch = (left.epochOffsetMs ?? 0) - (right.epochOffsetMs ?? 0);
  if (epoch !== 0) return epoch;
  if (left.trackId !== right.trackId) return left.trackId < right.trackId ? -1 : 1;
  if (left.sequence !== right.sequence) return left.sequence - right.sequence;
  return left.startMs - right.startMs;
}

function assertOwner(userId) {
  if (typeof userId !== "string" || userId.length === 0) {
    throw new StorageError("owner user id is required");
  }
}

function assertMeta(meta) {
  if (!meta || typeof meta !== "object") throw new StorageError("chunk metadata is required");
  for (const field of ["uploadSessionId", "captureEpochId", "trackId"]) {
    if (typeof meta[field] !== "string" || meta[field].length === 0) {
      throw new StorageError(`chunk metadata field is required: ${field}`);
    }
  }
  if (!Number.isInteger(meta.sequence) || meta.sequence < 0) {
    throw new StorageError("chunk sequence must be a non-negative integer");
  }
  if (!Number.isFinite(meta.startMs) || !Number.isFinite(meta.endMs) || meta.endMs < meta.startMs) {
    throw new StorageError("chunk time range is invalid");
  }
  if (typeof meta.checksum !== "string" || !HEX_CHECKSUM.test(meta.checksum)) {
    throw new StorageError("chunk checksum must be a 64 character hex digest");
  }
  if (meta.codec !== undefined && typeof meta.codec !== "string") {
    throw new StorageError("chunk codec must be a string");
  }
}

async function toBytes(bytes) {
  if (bytes instanceof Uint8Array) return new Uint8Array(bytes);
  if (bytes instanceof ArrayBuffer) return new Uint8Array(bytes.slice(0));
  if (ArrayBuffer.isView(bytes)) {
    return new Uint8Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  }
  if (typeof Blob !== "undefined" && bytes instanceof Blob) {
    return new Uint8Array(await bytes.arrayBuffer());
  }
  throw new StorageError("unsupported chunk payload");
}

function requestDone(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new StorageError("indexedDB request failed"));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new StorageError("indexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new StorageError("indexedDB transaction aborted"));
  });
}

function openDatabase(indexedDB, name) {
  return new Promise((resolve, reject) => {
    let request;
    try {
      request = indexedDB.open(name, DATABASE_VERSION);
    } catch (error) {
      reject(new StorageError("indexedDB open threw", error));
      return;
    }
    request.onupgradeneeded = (event) => {
      const database = event.target.result;
      if (!database.objectStoreNames.contains(CHUNK_STORE)) {
        const store = database.createObjectStore(CHUNK_STORE, { keyPath: KEY_PATH });
        store.createIndex(UPLOAD_INDEX, ["ownerUserId", "uploadSessionId"], { unique: false });
      }
      if (!database.objectStoreNames.contains(WARNING_STORE)) {
        database.createObjectStore(WARNING_STORE, { keyPath: WARNING_KEY_PATH });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new StorageError("indexedDB open failed", request.error));
    request.onblocked = () => reject(new StorageError("indexedDB open blocked"));
  });
}

function asStorageError(message, error) {
  if (error instanceof StorageError) return error;
  return new StorageError(error?.message ? `${message}: ${error.message}` : message, error);
}
