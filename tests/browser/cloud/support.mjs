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
    const tracks = [];
    if (constraints.video) {
      tracks.push(new FakeTrack("video", "fake camera", { width: 640, height: 480 }));
    }
    if (constraints.audio && this.mic.audio) {
      tracks.push(new FakeTrack("audio", "fake microphone", { sampleRate: 48000 }));
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
        new FakeTrack("audio", "fake shared system audio", { sampleRate: 48000 }),
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
