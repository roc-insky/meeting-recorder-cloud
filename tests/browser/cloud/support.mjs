// Synthetic MediaStream fixtures for the cloud capture probe.
// No real microphone, display, or meeting application is used here.

export class FakeClock {
  constructor(startMs = 0) {
    this.value = startMs;
  }

  now() {
    return this.value;
  }

  advance(deltaMs) {
    this.value += deltaMs;
    return this.value;
  }
}

export class FakeTrack {
  constructor(kind, label, settings = {}) {
    this.kind = kind;
    this.label = label;
    this.readyState = "live";
    this.enabled = true;
    this.muted = false;
    this.id = `${kind}-${label}`;
    this.settings = { ...settings };
  }

  getSettings() {
    return { ...this.settings };
  }

  stop() {
    this.readyState = "ended";
  }
}

export class FakeStream {
  constructor(tracks = []) {
    this.tracks = [...tracks];
  }

  getTracks() {
    return [...this.tracks];
  }

  getAudioTracks() {
    return this.tracks.filter((track) => track.kind === "audio");
  }

  getVideoTracks() {
    return this.tracks.filter((track) => track.kind === "video");
  }

  getTrackById(id) {
    return this.tracks.find((track) => track.id === id) ?? null;
  }
}

const DEFAULT_MIME = "audio/webm;codecs=opus";

export class FakeRecorder {
  static supported = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ];

  static isTypeSupported(type) {
    return type === "" || FakeRecorder.supported.includes(type);
  }

  constructor(stream, options = {}) {
    this.stream = stream;
    this.mimeType = options.mimeType ?? DEFAULT_MIME;
    this.state = "inactive";
    this.timesliceMs = null;
    this.chunks = [];
    this.ondataavailable = null;
    this.onstop = null;
    this.onerror = null;
  }

  start(timesliceMs) {
    if (this.state !== "inactive") {
      throw new Error("InvalidStateError: recorder already started");
    }
    this.state = "recording";
    this.timesliceMs = timesliceMs ?? null;
  }

  stop() {
    if (this.state === "inactive") {
      return;
    }
    this.state = "inactive";
    // Synthetic WebM-like header chunk; enough for the probe's continuity rules.
    const data = new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 0x01])]);
    this.chunks.push(data);
    this.ondataavailable?.({ data });
    this.onstop?.();
  }

  // Simulate a MediaRecorder timeslice firing while the recorder is running.
  emitData(bytes = new Uint8Array([0x1f, 0x43, 0xb6, 0x75, 0x01])) {
    if (this.state === "inactive") {
      throw new Error("InvalidStateError: recorder is not running");
    }
    const data = new Blob([bytes]);
    this.chunks.push(data);
    this.ondataavailable?.({ data });
  }

  pause() {
    if (this.state === "recording") {
      this.state = "paused";
    }
  }

  resume() {
    if (this.state === "paused") {
      this.state = "recording";
    }
  }
}

export class FakeMediaDevices {
  constructor({ mic = { audio: true }, display = null, recorderMimeType = null } = {}) {
    this.mic = { ...mic };
    this.display = display ? { ...display } : null;
    this.recorderMimeType = recorderMimeType;
    this.getUserMediaCalls = [];
    this.getDisplayMediaCalls = [];
    this.recorders = [];
    this.streams = [];
  }

  _stream(tracks) {
    const stream = new FakeStream(tracks);
    this.streams.push(stream);
    return stream;
  }

  async getUserMedia(constraints = {}) {
    this.getUserMediaCalls.push({ constraints });
    if (this.mic.error) {
      throw this.mic.error;
    }
    const tracks = [];
    if (constraints.video) {
      tracks.push(new FakeTrack("video", "fake camera", { width: 640, height: 480 }));
    }
    if (constraints.audio && this.mic.audio) {
      tracks.push(
        new FakeTrack("audio", "fake microphone", {
          sampleRate: this.mic.sampleRate ?? 48000,
        }),
      );
    }
    return this._stream(tracks);
  }

  async getDisplayMedia(constraints = {}) {
    this.getDisplayMediaCalls.push({ constraints });
    if (!this.display) {
      throw new Error("NotAllowedError: display capture is not configured");
    }
    const tracks = [];
    if (constraints.video && this.display.video) {
      tracks.push(new FakeTrack("video", "fake shared screen", { width: 1280, height: 720 }));
    }
    if (constraints.audio && this.display.audio) {
      tracks.push(
        new FakeTrack("audio", "fake shared system audio", {
          sampleRate: this.display.sampleRate ?? 48000,
        }),
      );
    }
    return this._stream(tracks);
  }

  createStream(tracks) {
    return this._stream(tracks);
  }

  createRecorder(stream, options = {}) {
    const recorder = new FakeRecorder(stream, {
      ...options,
      mimeType: this.recorderMimeType ?? options.mimeType ?? DEFAULT_MIME,
    });
    this.recorders.push(recorder);
    return recorder;
  }
}

export function recorderIsAudioOnly(recorder) {
  return recorder.stream.getTracks().every((track) => track.kind === "audio");
}

// ---------------------------------------------------------------------------
// In-memory IndexedDB subset
//
// Node 22 has no IndexedDB. The production ChunkBuffer only relies on a small
// subset (open/upgrade, compound keyPath stores, an index, put/delete/getAll),
// so the same module can be exercised in Node against this shim while the
// Playwright suite exercises real IndexedDB in Chromium.
// ---------------------------------------------------------------------------

function cloneValue(value) {
  if (typeof structuredClone === "function") {
    try {
      return structuredClone(value);
    } catch {
      return value;
    }
  }
  return value;
}

function keyFor(value) {
  return JSON.stringify(value);
}

function sameKey(a, b) {
  return keyFor(a) === keyFor(b);
}

function valueForKeyPath(keyPath, value) {
  if (Array.isArray(keyPath)) {
    return keyPath.map((part) => value[part]);
  }
  return value[keyPath];
}

class MemoryRequest {
  constructor() {
    this.result = undefined;
    this.error = null;
    this.onsuccess = null;
    this.onerror = null;
  }
}

class MemoryIndex {
  constructor(store, name, keyPath) {
    this.store = store;
    this.name = name;
    this.keyPath = keyPath;
  }

  getAll(query) {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      request.result = [...this.store.data.values()].filter((value) => {
        const indexKey = valueForKeyPath(this.keyPath, value);
        return query === undefined || sameKey(indexKey, query);
      });
      request.onsuccess?.({ target: request });
    });
    return request;
  }

  count(query) {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      request.result = [...this.store.data.values()].filter((value) => {
        const indexKey = valueForKeyPath(this.keyPath, value);
        return query === undefined || sameKey(indexKey, query);
      }).length;
      request.onsuccess?.({ target: request });
    });
    return request;
  }
}

class MemoryObjectStore {
  constructor(name, keyPath) {
    this.name = name;
    this.keyPath = keyPath;
    this.data = new Map();
    this.indexes = new Map();
  }

  createIndex(name, keyPath) {
    if (this.indexes.has(name)) {
      throw new Error(`index already exists: ${name}`);
    }
    const index = new MemoryIndex(this, name, keyPath);
    this.indexes.set(name, index);
    return index;
  }

  index(name) {
    const index = this.indexes.get(name);
    if (!index) throw new Error(`missing index: ${name}`);
    return index;
  }

  put(value) {
    const request = new MemoryRequest();
    const key = valueForKeyPath(this.keyPath, value);
    if (key === undefined || (Array.isArray(key) && key.some((part) => part === undefined))) {
      queueMicrotask(() => {
        request.error = new Error("DataError: missing key");
        request.onerror?.({ target: request });
      });
      return request;
    }
    const stored = cloneValue(value);
    this.data.set(keyFor(key), stored);
    queueMicrotask(() => {
      request.result = key;
      request.onsuccess?.({ target: request });
    });
    return request;
  }

  get(key) {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      request.result = this.data.get(keyFor(key));
      request.onsuccess?.({ target: request });
    });
    return request;
  }

  delete(key) {
    const request = new MemoryRequest();
    this.data.delete(keyFor(key));
    queueMicrotask(() => {
      request.result = undefined;
      request.onsuccess?.({ target: request });
    });
    return request;
  }

  getAll(query) {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      const values = [...this.data.values()];
      request.result =
        query === undefined
          ? values
          : values.filter((value) => sameKey(valueForKeyPath(this.keyPath, value), query));
      request.onsuccess?.({ target: request });
    });
    return request;
  }

  count() {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      request.result = this.data.size;
      request.onsuccess?.({ target: request });
    });
    return request;
  }
}

class MemoryTransaction {
  constructor(db, storeNames, mode) {
    this.db = db;
    this.mode = mode;
    this.objectStoreNames = [...storeNames];
    this.error = null;
    this.oncomplete = null;
    this.onerror = null;
    this.onabort = null;
    this._pending = 0;
    this._completed = false;
    queueMicrotask(() => this._maybeComplete());
  }

  objectStore(name) {
    if (!this.objectStoreNames.includes(name)) {
      throw new Error(`store not in transaction: ${name}`);
    }
    return this.db._store(name);
  }

  _track(request) {
    this._pending += 1;
    const originalSuccess = request.onsuccess;
    const originalError = request.onerror;
    request.onsuccess = (event) => {
      this._pending -= 1;
      try {
        originalSuccess?.(event);
      } finally {
        this._maybeComplete();
      }
    };
    request.onerror = (event) => {
      this._pending -= 1;
      this.error = request.error ?? new Error("transaction request failed");
      try {
        originalError?.(event);
      } finally {
        this.onerror?.({ target: this });
        this._maybeComplete();
      }
    };
    return request;
  }

  _maybeComplete() {
    if (this._completed || this._pending !== 0) return;
    // Requests are queued as microtasks; completion is a macrotask so every
    // request callback (including deletes issued from an index callback) has
    // run before oncomplete fires, matching the IndexedDB ordering contract.
    setTimeout(() => {
      if (this._completed || this._pending !== 0) return;
      this._completed = true;
      if (!this.error) this.oncomplete?.({ target: this });
    }, 0);
  }

  abort() {
    if (this._completed) return;
    this._completed = true;
    this.onabort?.({ target: this });
  }
}

export class MemoryDatabase {
  constructor(name, version) {
    this.name = name;
    this.version = version;
    this.stores = new Map();
  }

  get objectStoreNames() {
    return {
      contains: (name) => this.stores.has(name),
      [Symbol.iterator]: () => this.stores.keys(),
    };
  }

  createObjectStore(name, options = {}) {
    if (this.stores.has(name)) {
      throw new Error(`store already exists: ${name}`);
    }
    const store = new MemoryObjectStore(name, options.keyPath);
    this.stores.set(name, store);
    return store;
  }

  transaction(names, mode = "readonly") {
    const list = Array.isArray(names) ? names : [names];
    return new MemoryTransaction(this, list, mode);
  }

  _store(name) {
    const store = this.stores.get(name);
    if (!store) throw new Error(`missing store: ${name}`);
    return store;
  }

  close() {
    this.closed = true;
  }
}

export class MemoryIndexedDB {
  constructor() {
    this.databases = new Map();
  }

  open(name, version = 1) {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      let db = this.databases.get(name);
      const previous = db ? db.version : 0;
      if (!db || version > previous) {
        db = new MemoryDatabase(name, version);
        this.databases.set(name, db);
        request.result = db;
        request.transaction = { objectStore: (storeName) => db._store(storeName) };
        request.onupgradeneeded?.({
          target: { result: db, transaction: request.transaction },
          oldVersion: previous,
          newVersion: version,
        });
      }
      request.result = db;
      request.onsuccess?.({ target: request });
    });
    return request;
  }

  deleteDatabase(name) {
    const request = new MemoryRequest();
    queueMicrotask(() => {
      this.databases.delete(name);
      request.result = undefined;
      request.onsuccess?.({ target: request });
    });
    return request;
  }
}

export function pendingRequest(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = (event) => resolve(event?.target?.result ?? request.result);
    request.onerror = (event) =>
      reject(request.error ?? event?.target?.error ?? new Error("indexedDB request failed"));
  });
}

export function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(transaction.error ?? new Error("indexedDB transaction failed"));
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("indexedDB transaction aborted"));
  });
}

export async function waitFor(predicate, { timeoutMs = 2000, intervalMs = 5 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) {
      throw new Error("timed out waiting for condition");
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Upload doubles
// ---------------------------------------------------------------------------

export class FakeUploadTransport {
  constructor({ failures = 0, accept = true, error = null } = {}) {
    this.remainingFailures = failures;
    this.accept = accept;
    this.error = error;
    this.calls = [];
    this.accepted = [];
    this.events = [];
    this.sealCalls = [];
    this.sealResponse = null;
    this.sealError = null;
    this.onSend = null;
  }

  async seal(uploadId, manifest) {
    this.sealCalls.push({ uploadId, manifest });
    if (this.sealError) throw this.sealError;
    if (this.sealResponse) return this.sealResponse;
    return {
      input_version: this.accepted.length,
      status: manifest?.accept_partial ? "partial" : "complete",
      gaps: [],
    };
  }

  async putChunk({ meta, bytes }) {
    this.calls.push({ meta, size: bytes?.byteLength ?? bytes?.size ?? 0 });
    this.events.push("send");
    await this.onSend?.({ meta, bytes });
    if (this.remainingFailures > 0) {
      this.remainingFailures -= 1;
      throw this.error ?? new Error("network_down");
    }
    if (!this.accept) {
      return { accepted: false, duplicate: false, contiguous_ms: 0, missing: [] };
    }
    this.accepted.push(meta);
    return { accepted: true, duplicate: false, contiguous_ms: meta.endMs, missing: [] };
  }
}
