// Cloud meeting recorder — capture probe.
//
// This module is a spike, not a production API. It answers three questions
// about the browser side of dual audio capture:
//   1. Does getDisplayMedia actually hand us a system-audio track?
//   2. Can we keep the display video track alive without ever encoding it?
//   3. What does MediaRecorder produce, and do its chunks survive a reorder?
//
// Everything here works against an injected MediaDevices-like object so the
// same code can run against synthetic fixtures in Node and against a real
// browser from the probe page.

export const PROBE_VERSION = "0.1.0";

// 3 seconds keeps the upload unit small while staying above the 1 s timeslice
// that browsers may clamp and below the 5 s where a crash loses too much.
export const CHUNK_TARGET_MS = 3000;

const PREFERRED_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4",
];

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

// MediaRecorder support is dynamic: Safari, Chrome, and Firefox disagree, and
// the answer changes with codec packs. Never hard-code one container.
export function selectMimeType(RecorderCtor) {
  if (!RecorderCtor || typeof RecorderCtor.isTypeSupported !== "function") {
    return "";
  }
  for (const type of PREFERRED_MIME_TYPES) {
    try {
      if (RecorderCtor.isTypeSupported(type)) {
        return type;
      }
    } catch {
      // Some engines throw instead of returning false for unknown types.
    }
  }
  return "";
}

function defaultCreateStream(tracks) {
  if (typeof MediaStream === "undefined") {
    throw new Error("MediaStream is not available in this environment");
  }
  return new MediaStream(tracks);
}

function defaultCreateRecorder(stream) {
  const RecorderCtor = globalThis.MediaRecorder;
  if (!RecorderCtor) {
    throw new Error("MediaRecorder is not available in this environment");
  }
  const mimeType = selectMimeType(RecorderCtor);
  const options = { audioBitsPerSecond: 128000 };
  if (mimeType) {
    options.mimeType = mimeType;
  }
  return new RecorderCtor(stream, options);
}

function waitForRecorderStop(recorder) {
  if (!recorder || recorder.state === "inactive") {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const previousStop = recorder.onstop;
    recorder.onstop = () => {
      if (typeof previousStop === "function") {
        previousStop.call(recorder);
      }
      resolve();
    };
    recorder.onerror = (event) => {
      reject(
        (event && event.error) ||
          new Error("MediaRecorder failed while stopping the probe capture"),
      );
    };

    recorder.stop();
  });
}

/**
 * Probe one capture mode against an injected MediaDevices implementation.
 *
 * mode "mic"  — microphone only.
 * mode "dual" — microphone plus a display share. The display stream is asked
 *               for video and audio; the video track stays live so the share
 *               does not die, but it never enters an encoder.
 *
 * @returns {Promise<{
 *   dualReady: boolean,
 *   encodedSources: string[],
 *   codec: string,
 *   codecs: Record<string, string>,
 *   videoTrackLifecycle: string,
 *   chunkTargetMs: number,
 *   session: object,
 * }>}
 */
export async function probeCapture(mode, mediaDevices) {
  if (mode !== "mic" && mode !== "dual") {
    throw new TypeError(`unsupported capture mode: ${mode}; expected "mic" or "dual"`);
  }
  if (!mediaDevices || typeof mediaDevices.getUserMedia !== "function") {
    throw new TypeError("mediaDevices.getUserMedia is required to probe capture");
  }

  const makeStream =
    typeof mediaDevices.createStream === "function"
      ? mediaDevices.createStream.bind(mediaDevices)
      : defaultCreateStream;
  const makeRecorder =
    typeof mediaDevices.createRecorder === "function"
      ? mediaDevices.createRecorder.bind(mediaDevices)
      : defaultCreateRecorder;

  const micStream = await mediaDevices.getUserMedia({ audio: true, video: false });
  const micAudioTracks =
    typeof micStream.getAudioTracks === "function" ? micStream.getAudioTracks() : [];

  let displayStream = null;
  let systemAudioTracks = [];
  let videoTracks = [];
  if (mode === "dual") {
    if (typeof mediaDevices.getDisplayMedia !== "function") {
      throw new TypeError("mediaDevices.getDisplayMedia is required for dual mode");
    }
    // Do not request video:false: most engines refuse display capture without
    // a video track, and the track is what keeps the share alive. It is
    // deliberately never serialized below.
    displayStream = await mediaDevices.getDisplayMedia({ video: true, audio: true });
    systemAudioTracks =
      typeof displayStream.getAudioTracks === "function"
        ? displayStream.getAudioTracks()
        : [];
    videoTracks =
      typeof displayStream.getVideoTracks === "function"
        ? displayStream.getVideoTracks()
        : [];
  }

  const sources = [];
  if (micAudioTracks.length > 0) {
    sources.push({ name: "mic", tracks: micAudioTracks });
  }
  if (systemAudioTracks.length > 0) {
    sources.push({ name: "system", tracks: systemAudioTracks });
  }

  const recorders = { mic: null, system: null };
  const streams = { mic: null, system: null };
  const codecs = {};
  for (const source of sources) {
    // Each source keeps its own encoder and its own stream: mixing them would
    // throw away the per-track continuity evidence this probe exists to get.
    const stream = makeStream(source.tracks);
    const video = typeof stream.getVideoTracks === "function" ? stream.getVideoTracks() : [];
    if (video.length > 0) {
      throw new Error("refusing to encode a video track: only audio may reach MediaRecorder");
    }
    const recorder = makeRecorder(stream);
    recorders[source.name] = recorder;
    streams[source.name] = stream;
    codecs[source.name] = recorder.mimeType || "browser-default";
  }

  const encodedSources = sources.map((source) => source.name);
  const dualReady =
    mode === "dual" && micAudioTracks.length > 0 && systemAudioTracks.length > 0;
  const videoTrackLifecycle =
    videoTracks.length > 0
      ? "kept_live_unencoded"
      : displayStream
        ? "display_stream_without_video"
        : "no_video_track";
  const codec =
    encodedSources.length > 0 ? codecs[encodedSources[0]] : "unavailable";

  const session = {
    mode,
    recorders,
    streams,
    micStream,
    displayStream,
    micAudioTracks,
    systemAudioTracks,
    videoTracks,
    chunkTargetMs: CHUNK_TARGET_MS,
    startedAt: null,
    // Chunks are collected as they arrive. Attaching the handler only at stop
    // time loses every timeslice chunk emitted during the recording.
    collected: { mic: [], system: [] },

    async start() {
      const active = Object.entries(recorders).filter(([, recorder]) => recorder);
      if (active.length === 0) {
        throw new Error("no audio tracks available; refusing to start an empty capture");
      }
      const started = {};
      for (const [name, recorder] of active) {
        const previousData = recorder.ondataavailable;
        recorder.ondataavailable = (event) => {
          if (event && event.data && event.data.size !== 0) {
            this.collected[name].push(event.data);
          }
          if (typeof previousData === "function") {
            previousData.call(recorder, event);
          }
        };
        recorder.start(CHUNK_TARGET_MS);
        started[name] = {
          codec: recorder.mimeType || "browser-default",
          timesliceMs: CHUNK_TARGET_MS,
        };
      }
      this.startedAt = Date.now();
      return started;
    },

    /**
     * Stop every encoder, then release every track — including the display
     * video track, which stayed live only for the duration of the probe.
     * Returns one chunk list per source, in arrival order.
     */
    async stop() {
      for (const recorder of Object.values(recorders)) {
        await waitForRecorderStop(recorder);
      }
      const chunks = {
        mic: [...this.collected.mic],
        system: [...this.collected.system],
      };
      for (const track of [
        ...micAudioTracks,
        ...systemAudioTracks,
        ...videoTracks,
      ]) {
        try {
          track.stop();
        } catch (error) {
          // A track may already have ended; teardown must not mask that.
        }
      }
      for (const stream of Object.values(streams)) {
        if (stream && typeof stream.getTracks === "function") {
          for (const track of stream.getTracks()) {
            try {
              track.stop();
            } catch (error) {
              // Ignore an already-ended track during teardown.
            }
          }
        }
      }
      return chunks;
    },
  };

  return {
    dualReady,
    encodedSources,
    codec,
    codecs,
    videoTrackLifecycle,
    chunkTargetMs: CHUNK_TARGET_MS,
    session,
  };
}

export function describeProbeFailure(error) {
  return {
    ok: false,
    error: errorMessage(error),
    hint:
      "Dual capture needs a display share whose audio checkbox is ticked, plus microphone permission.",
  };
}
