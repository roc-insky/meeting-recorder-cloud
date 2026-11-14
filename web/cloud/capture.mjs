// Per-source MediaRecorder capture for the cloud meeting recorder.
//
// One recorder per audio source (microphone, system audio). The video track from
// a screen share stays live but is never encoded. Chunks are handed to the
// caller as WebM/Opus blobs; the caller is responsible for durable buffering.
// Node 22 compatible: uses only standard Web APIs, all injectable.

export const CHUNK_TARGET_MS = 3000;

export const PREFERRED_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4",
];

export function selectMimeType(RecorderCtor = globalThis.MediaRecorder) {
  if (!RecorderCtor) return "";
  if (typeof RecorderCtor.isTypeSupported !== "function") return "";
  for (const type of PREFERRED_MIME_TYPES) {
    try {
      if (RecorderCtor.isTypeSupported(type)) return type;
    } catch {
      // A broken isTypeSupported must not prevent the remaining fallbacks.
    }
  }
  return "";
}

const STOP_TIMEOUT_MS = 2000;

export async function startCapture(options = {}) {
  const {
    mode = "mic",
    epoch = {},
    tracks: requestedTracks = null,
    mediaDevices = globalThis.navigator?.mediaDevices,
    clock = { now: () => globalThis.performance?.now?.() ?? Date.now() },
    onChunk = async () => {},
    onState = () => {},
    chunkTargetMs = CHUNK_TARGET_MS,
    RecorderCtor = globalThis.MediaRecorder,
  } = options;

  if (!mediaDevices) {
    throw new Error("mediaDevices is unavailable");
  }

  const epochId = epoch.id ?? "epoch";
  const offsetMs = Number.isFinite(epoch.offsetMs) ? epoch.offsetMs : 0;
  const anchorMs = Number.isFinite(epoch.anchorMs) ? epoch.anchorMs : clock.now();

  const state = { state: "idle", error: null, reason: null, startedAt: null, shareError: null };
  let micStream = null;
  let displayStream = null;
  let chain = Promise.resolve();
  let stopPromise = null;
  let recorderError = null;
  let stopping = false;

  function emit(event) {
    if (event.state) state.state = event.state;
    if (event.error !== undefined) state.error = event.error;
    if (event.reason !== undefined) state.reason = event.reason;
    try {
      onState({ ...event });
    } catch {
      // A UI callback must never break capture.
    }
  }

  function stopStreams() {
    for (const stream of [micStream, displayStream]) {
      for (const track of stream?.getTracks?.() ?? []) {
        try {
          track.stop();
        } catch {
          // Already ended.
        }
      }
    }
  }

  function fail(error, forcedState = null) {
    stopStreams();
    const denied = error?.name === "NotAllowedError" || error?.name === "SecurityError";
    state.state = forcedState ?? (denied ? "permission_denied" : "capture_failed");
    state.error = error?.name ?? "capture_failed";
    state.reason = error?.message ?? String(error);
    emit({ state: state.state, error: state.error, reason: state.reason });
    throw error;
  }

  function shareFailure(error) {
    state.shareError = {
      error: error?.name ?? "capture_failed",
      reason: error?.message ?? String(error),
    };
    return state.shareError;
  }

  emit({ state: "requesting_permissions" });

  // A denied microphone always aborts: there would be nothing to record.
  if (mode === "mic" || mode === "dual") {
    try {
      micStream = await mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (error) {
      fail(error);
    }
  }

  async function openDisplay() {
    if (typeof mediaDevices.getDisplayMedia !== "function") {
      throw Object.assign(new Error("display capture is unavailable"), {
        name: "NotSupportedError",
      });
    }
    return mediaDevices.getDisplayMedia({ video: true, audio: true });
  }

  // In dual mode the microphone is the recording of last resort. A refused or
  // silent screen share must not destroy the microphone recording; the caller
  // can ask the user to share again with requestSystem().
  if (mode === "dual" || mode === "system") {
    try {
      displayStream = await openDisplay();
    } catch (error) {
      if (mode === "system") fail(error);
      shareFailure(error);
    }
  }

  const audioSources = [];
  if ((mode === "mic" || mode === "dual") && micStream) {
    for (const track of micStream.getAudioTracks()) {
      audioSources.push({ source: "mic", track, stream: micStream });
    }
  }
  if ((mode === "dual" || mode === "system") && displayStream) {
    for (const track of displayStream.getAudioTracks()) {
      audioSources.push({ source: "system", track, stream: displayStream });
    }
  }

  if (audioSources.length === 0) {
    const reason =
      mode === "system" || mode === "dual"
        ? "system_audio_unavailable"
        : "no_microphone_audio_track";
    const error = new Error(reason);
    error.name = reason;
    fail(error);
  }

  const mimeType = selectMimeType(RecorderCtor);
  const codec = mimeType || "audio/webm";
  const perSourceCount = new Map();
  const specs = [];
  const recorders = [];
  const videoTracks = [];
  for (const stream of [micStream, displayStream]) {
    for (const track of stream?.getVideoTracks?.() ?? []) {
      videoTracks.push(track);
    }
  }

  function makeRecorder(stream) {
    if (typeof mediaDevices.createRecorder === "function") {
      return mediaDevices.createRecorder(stream, mimeType ? { mimeType } : {});
    }
    if (!RecorderCtor) throw new Error("MediaRecorder is unavailable");
    const isolated =
      typeof globalThis.MediaStream === "function" ? new globalThis.MediaStream(stream.getTracks()) : stream;
    try {
      return new RecorderCtor(isolated, mimeType ? { mimeType } : undefined);
    } catch {
      return new RecorderCtor(isolated);
    }
  }

  function buildSpec(entry, cursorMs) {
    const seen = (perSourceCount.get(entry.source) ?? 0) + 1;
    perSourceCount.set(entry.source, seen);
    const settings = entry.track.getSettings?.() ?? {};
    const sampleRate = Number.isFinite(settings.sampleRate) ? settings.sampleRate : null;
    return {
      id: seen === 1 ? entry.source : `${entry.source}-${seen}`,
      source: entry.source,
      codec,
      sample_rate: sampleRate,
      sample_rate_source: sampleRate === null ? "unknown" : "track_settings",
      track: entry.track,
      stream: entry.stream,
      sequence: 0,
      cursorMs,
    };
  }

  function audioOnlyStream(track) {
    if (typeof globalThis.MediaStream === "function") {
      return new globalThis.MediaStream([track]);
    }
    // Node has no MediaStream. A minimal audio-only view keeps the invariant
    // that a shared video track can never reach a MediaRecorder.
    return {
      getTracks: () => [track],
      getAudioTracks: () => (track.kind === "audio" ? [track] : []),
      getVideoTracks: () => (track.kind === "video" ? [track] : []),
    };
  }

  function attachRecorder(spec) {
    const streamForRecorder = audioOnlyStream(spec.track);
    const recorder = makeRecorder(streamForRecorder);
    if (typeof recorder.mimeType === "string" && recorder.mimeType) {
      spec.codec = recorder.mimeType;
    }
    const recordersForTrack = { spec, recorder, stopped: false };

    recorder.ondataavailable = (event) => {
      const data = event?.data;
      const size = data?.size ?? data?.byteLength ?? 0;
      if (!data || size === 0) return;
      const startMs = spec.cursorMs;
      const observed = offsetMs + (clock.now() - anchorMs);
      const endMs = Math.max(startMs, observed);
      spec.cursorMs = endMs;
      const meta = {
        trackId: spec.id,
        source: spec.source,
        sequence: spec.sequence,
        startMs,
        endMs,
        codec: spec.codec,
        sampleRate: spec.sample_rate,
        byteLength: size,
        timesliceMs: chunkTargetMs,
      };
      spec.sequence += 1;
      chain = chain
        .then(() => onChunk({ meta, bytes: data, trackSpec: publicSpec(spec) }))
        .catch((error) => {
          recorderError = error;
        });
    };

    recorder.onerror = (event) => {
      const error = event?.error ?? event;
      recorderError = error;
      emit({ state: "recorder_error", error: error?.name ?? "recorder_error", reason: error?.message });
    };

    recorders.push(recordersForTrack);
    return recordersForTrack;
  }

  const initialSpecs = audioSources.map((entry) => buildSpec(entry, offsetMs));
  const requestedIds = requestedTracks?.length
    ? new Set(requestedTracks.map((track) => track.id ?? track))
    : null;
  const selected = requestedIds
    ? initialSpecs.filter((spec) => requestedIds.has(spec.id))
    : initialSpecs;
  if (selected.length === 0) {
    const error = new Error("no_matching_track");
    error.name = "no_matching_track";
    fail(error);
  }
  specs.push(...selected);
  for (const spec of selected) attachRecorder(spec);

  try {
    for (const { recorder } of recorders) {
      recorder.start(chunkTargetMs);
    }
  } catch (error) {
    for (const { recorder } of recorders) {
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch {
        // Best effort.
      }
    }
    fail(error, "capture_failed");
  }

  state.startedAt = clock.now();
  emit({
    state: "recording",
    tracks: specs.map(publicSpec),
    mimeType,
    epochId,
    offsetMs,
    shareError: state.shareError,
  });

  async function requestSystem() {
    if (stopping || state.state === "stopped") throw new Error("capture_stopped");
    if (displayStream?.getAudioTracks?.().length) {
      return { tracks: specsNow(), shareError: null };
    }
    let stream;
    try {
      stream = await openDisplay();
    } catch (error) {
      const failure = shareFailure(error);
      emit({ state: "system_unavailable", ...failure });
      throw error;
    }
    displayStream = stream;
    for (const track of stream.getVideoTracks?.() ?? []) {
      videoTracks.push(track);
    }
    const systemTracks = stream.getAudioTracks();
    if (systemTracks.length === 0) {
      const error = new Error("system_audio_unavailable");
      error.name = "system_audio_unavailable";
      const failure = shareFailure(error);
      emit({ state: "system_unavailable", ...failure });
      throw error;
    }
    const cursorMs = offsetMs + Math.max(0, clock.now() - anchorMs);
    for (const track of systemTracks) {
      const spec = buildSpec({ source: "system", track, stream }, cursorMs);
      specs.push(spec);
      try {
        attachRecorder(spec);
        recorders.at(-1).recorder.start(chunkTargetMs);
      } catch (error) {
        fail(error, "capture_failed");
      }
    }
    state.shareError = null;
    emit({
      state: "system_joined",
      tracks: specsNow(),
      mimeType,
      epochId,
      offsetMs,
      shareError: null,
    });
    return { tracks: specsNow(), shareError: null };
  }

  async function stop() {
    if (stopPromise) return stopPromise;
    if (state.state === "stopped") return undefined;
    stopping = true;
    emit({ state: "stopping" });
    stopPromise = (async () => {
      for (const entry of recorders) {
        if (entry.stopped || entry.recorder.state === "inactive") {
          entry.stopped = true;
          continue;
        }
        entry.stopped = true;
        await new Promise((resolve) => {
          let settled = false;
          const finish = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve();
          };
          const timer = setTimeout(finish, STOP_TIMEOUT_MS);
          entry.recorder.onstop = finish;
          try {
            entry.recorder.stop();
          } catch {
            finish();
          }
        });
      }
      await chain.catch(() => {});
      stopStreams();
      state.state = "stopped";
      emit({ state: "stopped", error: recorderError?.name ?? null });
    })();
    return stopPromise;
  }

  function specsNow() {
    return specs.map(publicSpec);
  }

  function publicSpec(spec) {
    const readyState = spec.track?.readyState ?? "unknown";
    return {
      id: spec.id,
      source: spec.source,
      codec: spec.codec,
      sample_rate: spec.sample_rate,
      sample_rate_source: spec.sample_rate_source,
      readyState,
    };
  }

  return {
    get state() {
      return state.state;
    },
    get error() {
      return recorderError;
    },
    get shareError() {
      return state.shareError;
    },
    epochId,
    offsetMs,
    mimeType,
    trackSpecs: specsNow,
    specs: specsNow,
    liveTracks: () => [...videoTracks, ...specs.map((spec) => spec.track)],
    systemAvailable: () => specs.some((spec) => spec.source === "system"),
    requestSystem,
    stop,
  };
}
