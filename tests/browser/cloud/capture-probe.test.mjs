import assert from "node:assert/strict";
import test from "node:test";

import {
  CHUNK_TARGET_MS,
  probeCapture,
  selectMimeType,
} from "../../../spike/cloud_capture/capture.mjs";
import {
  FakeMediaDevices,
  FakeRecorder,
  recorderIsAudioOnly,
} from "./support.mjs";

test("rejects_missing_system_audio", async () => {
  const devices = new FakeMediaDevices({
    mic: { audio: true },
    display: { video: true, audio: false },
  });

  const result = await probeCapture("dual", devices);

  assert.equal(result.dualReady, false, "video-only share must not count as dual audio");
  assert.deepEqual(result.encodedSources, ["mic"]);
  assert.ok(!result.encodedSources.includes("video"));
  assert.equal(result.session.recorders.system, null);
  assert.equal(result.session.recorders.mic.state, "inactive");

  const recorder = result.session.recorders.mic;
  assert.equal(recorderIsAudioOnly(recorder), true);
  assert.deepEqual(
    recorder.stream.getTracks().map((track) => track.kind),
    ["audio"],
  );
  assert.equal(recorder.stream.getVideoTracks().length, 0);

  // The share still needs its video track for the display session, but it must
  // stay outside the encoder. It is released only when the probe tears down.
  assert.equal(result.videoTrackLifecycle, "kept_live_unencoded");
  assert.equal(result.session.videoTracks.length, 1);
  assert.equal(result.session.videoTracks[0].readyState, "live");
  assert.deepEqual(devices.getDisplayMediaCalls[0].constraints.audio, true);
  assert.notEqual(devices.getDisplayMediaCalls[0].constraints.video, false);

  await result.session.stop();
  assert.equal(result.session.videoTracks[0].readyState, "ended");
});

test("never_serializes_video", async () => {
  const devices = new FakeMediaDevices({
    mic: { audio: true },
    display: { video: true, audio: true },
    recorderMimeType: "audio/webm;codecs=opus",
  });

  const result = await probeCapture("dual", devices);

  assert.equal(result.dualReady, true);
  assert.deepEqual(result.encodedSources, ["mic", "system"]);
  assert.equal(result.codec, "audio/webm;codecs=opus");
  assert.match(result.codec, /^audio\//);

  const { mic, system } = result.session.recorders;
  assert.ok(mic, "microphone encoder exists");
  assert.ok(system, "system-audio encoder exists");
  assert.notEqual(mic, system, "each source keeps its own encoder");
  assert.equal(recorderIsAudioOnly(mic), true);
  assert.equal(recorderIsAudioOnly(system), true);
  assert.equal(mic.stream.getVideoTracks().length, 0);
  assert.equal(system.stream.getVideoTracks().length, 0);
  assert.deepEqual(
    mic.stream.getAudioTracks().map((track) => track.label),
    ["fake microphone"],
  );
  assert.deepEqual(
    system.stream.getAudioTracks().map((track) => track.label),
    ["fake shared system audio"],
  );

  assert.equal(result.session.videoTracks[0].readyState, "live");
  await result.session.start();
  assert.equal(mic.state, "recording");
  assert.equal(system.state, "recording");
  assert.equal(mic.timesliceMs, CHUNK_TARGET_MS);
  assert.equal(system.timesliceMs, CHUNK_TARGET_MS);

  const chunks = await result.session.stop();
  assert.ok(chunks.mic.length >= 1);
  assert.ok(chunks.system.length >= 1);
  assert.equal(mic.stream.getVideoTracks().length, 0);
  assert.equal(system.stream.getVideoTracks().length, 0);
  assert.equal(result.session.videoTracks[0].readyState, "ended");
  assert.equal(mic.state, "inactive");
  assert.equal(system.state, "inactive");
});

test("keeps_timeslice_chunks_emitted_before_stop", async () => {
  const devices = new FakeMediaDevices({
    mic: { audio: true },
    display: { video: true, audio: true },
  });

  const result = await probeCapture("dual", devices);
  await result.session.start();

  const { mic, system } = result.session.recorders;
  mic.emitData(new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 0x01]));
  system.emitData(new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 0x02]));

  const chunks = await result.session.stop();
  assert.equal(chunks.mic.length, 2, "timeslice chunk plus the final chunk");
  assert.equal(chunks.system.length, 2);
  assert.equal(chunks.mic[0].size, 5);
  assert.equal(chunks.system[0].size, 5);
});

test("mic_mode_never_touches_display_capture", async () => {
  const devices = new FakeMediaDevices({ mic: { audio: true } });

  const result = await probeCapture("mic", devices);

  assert.equal(devices.getDisplayMediaCalls.length, 0);
  assert.equal(result.dualReady, false);
  assert.deepEqual(result.encodedSources, ["mic"]);
  assert.equal(result.videoTrackLifecycle, "no_video_track");
  assert.equal(result.session.recorders.mic.stream.getVideoTracks().length, 0);

  await result.session.stop();
});

test("unavailable_system_audio_is_reported_not_guessed", async () => {
  const devices = new FakeMediaDevices({
    mic: { audio: true },
    display: { video: true, audio: false },
  });

  const result = await probeCapture("dual", devices);
  assert.equal(result.dualReady, false);
  assert.equal(result.session.systemAudioTracks.length, 0);
  assert.equal(result.session.videoTracks.length, 1);
  assert.deepEqual(result.encodedSources, ["mic"]);

  await result.session.stop();
});

test("unknown_mode_is_rejected_before_any_capture", async () => {
  const devices = new FakeMediaDevices({ mic: { audio: true } });

  await assert.rejects(() => probeCapture("everything", devices), /mode/i);
  assert.equal(devices.getUserMediaCalls.length, 0);
  assert.equal(devices.getDisplayMediaCalls.length, 0);
});

test("select_mime_prefers_webm_opus", () => {
  assert.equal(selectMimeType(FakeRecorder), "audio/webm;codecs=opus");
  assert.equal(
    selectMimeType({ isTypeSupported: (type) => type === "audio/ogg;codecs=opus" }),
    "audio/ogg;codecs=opus",
  );
  assert.equal(selectMimeType({ isTypeSupported: () => false }), "");
});
