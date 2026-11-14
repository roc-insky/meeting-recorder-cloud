# Capture probe evidence (Task 2)

**Status: SYNTHETIC ONLY — no real OS / browser / meeting-application capture was run.**
The rows in the Step 4 real-machine matrix remain `not_run` (see the end of this file and
`capture-contract.json`). Nothing here is a compatibility claim.

Date: 2026-11-14

## What was exercised

A real Google Chrome 157 process loaded the probe page and ran the dual-source path against
**injected synthetic MediaStreams**. This exercises the JavaScript, MediaRecorder, media-track
and container behaviour; it does **not** exercise OS audio routing, a real meeting application,
speaker/headphone playback, or a real screen-share picker.

| Item | Value |
|---|---|
| Host OS | macOS 26.2 (build 25C56) |
| Browser | Google Chrome 157.0.8103.52, Playwright channel `chrome`, headless |
| Driver | playwright-core 1.65.1, installed temporarily in `/tmp/cloud-probe-pw` (not a repo dependency) |
| Node | v24.14.0 |
| Python | 3.9.6 (`python3`) |
| Microphone stream | WebAudio `MediaStreamAudioDestinationNode`, 440 Hz oscillator |
| Display stream | `canvas.captureStream(5)` 320x180 video **plus** WebAudio 660 Hz audio track |
| Recording length | ~7 s wall clock (3 timeslice chunks per track) |
| Upload target | local probe server `http://127.0.0.1:8791` |

Synthetic sources only; no real microphone, no real screen, no meeting software, and no
committed recording.

### Probe page flow

1. `#probe-dual` requested `getUserMedia({audio:true,video:false})` and
   `getDisplayMedia({video:true,audio:true})`.
2. The page reported:
   `Dual audio ready: microphone and shared system audio are both present.`
3. Start, wait ~7 s, stop. The page reported:
   `Stopped. 6 chunk(s) held in memory; tracks released.`
4. Both tracks were uploaded in reverse order (sequence 2, 1, 0) to test server-side reordering.

### Observed capture result

- `dualReady = true`
- `encodedSources = ["mic", "system"]`
- `codec = audio/webm;codecs=opus` for both sources
- `chunkTargetMs = 3000`
- `videoTrackLifecycle = kept_live_unencoded`
- Video track present (`320x180`, 5 fps), `readyState: live`, `encoded: false` for the whole
  recording; it was ended together with the audio tracks at stop.
- No video frame was passed to a MediaRecorder, uploaded, stored, or logged. `capture.mjs`
  refuses an encoder stream that contains a video track
  (`refusing to encode a video track: only audio may reach MediaRecorder`), and
  `never_serializes_video` asserts all encoder inputs are audio-only.

Chunk sizes and SHA-256 of the reconstructed streams (both tracks produced identical byte
counts because the synthetic tones are encoded for the same duration):

| Track | Chunk 0 | Chunk 1 | Chunk 2 | Reconstructed | SHA-256 |
|---|---|---|---|---|---|
| mic | 42,666 B | 48,316 B | 16,437 B | 107,419 B | `a8185c80c73015e3dcbbf85a8beb02078e6b880f0d301dd1c7b157bb137b4903` |
| system | 42,666 B | 48,316 B | 16,437 B | 107,419 B | `995380f8a5d7289d1260f7f861db4ac4cef443e0592209d564c8f8263fd5b7c5` |

The server returned 200 for all six uploads regardless of arrival order, reported
`sequences: [0,1,2]` and `missing: []`, and reconstructed bytes whose SHA-256 matched the
browser-side concatenation exactly.

## Automated tests behind this evidence

```
node --test tests/browser/cloud/capture-probe.test.mjs
# 7 tests, 7 pass, 0 fail, exit 0
```

Covered negative/positive cases:

- `rejects_missing_system_audio`: video-only share => `dualReady = false`, only the mic is
  encoded, `session.recorders.system === null`, display request still asks for audio.
- `never_serializes_video`: encoded inputs are audio-only, one recorder per source, no video
  track reaches MediaRecorder.
- `keeps_timeslice_chunks_emitted_before_stop`: chunks emitted by the 3 s timeslice are kept,
  not only the final `stop` chunk. (This is a regression test for a real bug found in Chrome:
  attaching `ondataavailable` only at stop lost every timeslice chunk and produced one
  headerless cluster.)
- `mic_mode_never_touches_display_capture`.
- `unavailable_system_audio_is_reported_not_guessed`.
- `unknown_mode_is_rejected_before_any_capture`.
- `select_mime_prefers_webm_opus`.

Python upload tests are covered in [upload.md](upload.md).

## What this evidence proves

- With synthetic streams, Chrome can keep a display video track alive while encoding only the
  two audio tracks separately.
- `getDisplayMedia` audio is treated as optional; `dualReady` is false and the UI does not
  claim dual capture when the share has no audio track.
- The selected MediaRecorder format is `audio/webm;codecs=opus`, and the 3 s timeslice yields
  usable chunks.
- Chunks are **not** independently decodable; reconstruction requires the header-carrying first
  chunk plus ordered concatenation (see upload.md).
- The fully reconstructed stream is decodable by Chrome itself (6.6 s duration, playback
  advanced, no media error).

## What this evidence does NOT prove

- That any real OS/browser/meeting-app combination can capture system audio at all.
- That a specific meeting application's audio is routed into the tab/desktop share.
- Speaker vs headphone capture, echo, duplicate pickup, drift, or two-track alignment.
- Long sessions (2 h), network loss for 30 s, refresh/crash recovery, or share-stop handling.
- Real-time decode latency or the AudioWorklet PCM alternative.
- Safari, Firefox, Edge, Windows, Linux, or mobile behaviour.
- The design-derived epoch/gap rules (not exercised end to end).

## Step 4 real-machine matrix — all `not_run`

No Windows host, no Linux host, no real meeting software, and no real browser/system-audio
authorization can be driven in this environment. Recording these as passed would be false.

| Combination | Microphone | Browser-meeting tab audio | Desktop app / system audio | Status |
|---|---|---|---|---|
| Windows + Chrome/Edge | not_run | not_run | not_run | not_run |
| macOS + Chrome/Edge | not_run | not_run | not_run (design says high risk) | not_run |
| macOS + Safari | not_run | not_run | not_run | not_run |
| Windows/macOS + Firefox | not_run | not_run | not_run | not_run |
| Linux + Chrome/Edge | not_run | not_run | not_run | not_run |
| Mobile/tablet | not_run | not_run | not_run | not_run |

Reason for every row: no real OS/browser/meeting/app/audio-route combination was available;
only synthetic MediaStreams were used. Also not measured: OS/browser/meeting versions,
share target, speaker/headphone route, correct audio content, recording duration, drift, and
decode latency.

## Reproduction

```
# probe server (from repo root)
python3 -m spike.cloud_capture.probe_server --root spike/cloud_capture \
  --out-dir /tmp/cloud-probe-chunks --host 127.0.0.1 --port 8791

# temporary driver, outside the repo
mkdir -p /tmp/cloud-probe-pw && cd /tmp/cloud-probe-pw
npm init -y && npm install playwright-core@1.65.1
node probe.mjs   # injects navigator.mediaDevices fakes, drives page, writes /tmp/browser-probe.json
```

Raw driver output: `/tmp/browser-probe.json` and `/tmp/browser-probe-stdout.txt` (temporary,
not committed). The page is also runnable by hand at `http://127.0.0.1:8791/`.

## Design-document note

The brief refers to design section 19 ("unverified fields"), but
`docs/product/cloud-meeting-recorder-design.md` contains sections 0–18 only. The unverified
fields below are therefore taken from §5.3 (support matrix) and §16 (acceptance/probe plan)
instead. This discrepancy is reported in the task report.
