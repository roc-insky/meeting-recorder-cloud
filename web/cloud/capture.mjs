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

  const state = { state: "idle", error: null, reason: null, startedAt: null };
  let micStream = null;
  let displayStream = null;
  let chain = Promise.resolve();
  let stopPromise = null;
  let recorderError = null;

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

  emit({ state: "requesting_permissions" });

  try {
    if (mode === "mic" || mode === "dual") {
      micStream = await mediaDevices.getUserMedia({ audio: true, video: false });
    }
    if (mode === "dual" || mode === "system") {
      if (typeof mediaDevices.getDisplayMedia !== "function") {
        throw Object.assign(new Error("display capture is unavailable"), {
          name: "NotSupportedError",
        });
      }
      displayStream = await mediaDevices.getDisplayMedia({ video: true, audio: true });
    }
  } catch (error) {
    stopStreams();
    const denied = error?.name === "NotAllowedError" || error?.name === "SecurityError";
    state.state = denied ? "permission_denied" : "capture_failed";
    state.error = error?.name ?? "capture_failed";
    state.reason = error?.message ?? String(error);
    emit({ state: state.state, error: state.error, reason: state.reason });
    throw error;
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
    stopStreams();
    const reason =
      mode === "system" || mode === "dual"
        ? "system_audio_unavailable"
        : "no_microphone_audio_track";
    state.state = "capture_failed";
    state.error = reason;
    state.reason = "no audio track was produced";
    emit({ state: state.state, error: state.error, reason: state.reason });
    throw new Error(reason);
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

  for (const entry of audioSources) {
    const seen = (perSourceCount.get(entry.source) ?? 0) + 1;
    perSourceCount.set(entry.source, seen);
    const settings = entry.track.getSettings?.() ?? {};
    const sampleRate = Number.isFinite(settings.sampleRate) ? settings.sampleRate : null;
    const spec = {
      id: seen === 1 ? entry.source : `${entry.source}-${seen}`,
      source: entry.source,
      codec,
      sample_rate: sampleRate,
      sample_rate_source: sampleRate === null ? "unknown" : "track_settings",
      track: entry.track,
      sequence: 0,
      cursorMs: offsetMs,
    };
    specs.push(spec);
  }

  const requestedIds = requestedTracks?.length
    ? new Set(requestedTracks.map((track) => track.id ?? track))
    : null;
  const selected = requestedIds ? specs.filter((spec) => requestedIds.has(spec.id)) : specs;
  if (selected.length === 0) {
    stopStreams();
    state.state = "capture_failed";
    state.error = "no_matching_track";
    emit({ state: state.state, error: state.error, reason: state.error });
    throw new Error("no_matching_track");
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

  for (const spec of selected) {
    const streamForRecorder =
      typeof globalThis.MediaStream === "function" ? new globalThis.MediaStream([spec.track]) : spec.stream;
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

    recordersForTrack.recorder = recorder;
    recorders.push(recordersForTrack);
  }

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
    stopStreams();
    state.state = "capture_failed";
    state.error = error?.name ?? "recorder_start_failed";
    state.reason = error?.message ?? String(error);
    emit({ state: state.state, error: state.error, reason: state.reason });
    throw error;
  }

  state.startedAt = clock.now();
  emit({
    state: "recording",
    tracks: specs.map(publicSpec),
    mimeType,
    epochId,
    offsetMs,
  });

  async function stop() {
    if (stopPromise) return stopPromise;
    if (state.state === "stopped") return undefined;
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
    return selected.map(publicSpec);
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
    epochId,
    offsetMs,
    mimeType,
    trackSpecs: specsNow,
    specs: specsNow,
    liveTracks: () => [...videoTracks, ...selected.map((spec) => spec.track)],
    stop,
  };
}
