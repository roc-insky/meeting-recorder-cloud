# Upload / continuity probe evidence (Task 2)

**Status: SYNTHETIC / LOCAL SERVER ONLY.** No real network, storage service, meeting app, or
real recording was used. Network-loss, refresh, and share-stop scenarios are `not_run`.

Date: 2026-11-14

## Scope

This probe answers three narrow questions:

1. Can chunks that arrive out of order be reassembled to the exact original bytes?
2. Are MediaRecorder chunks independently decodable, or do they depend on a header / earlier
   chunks?
3. Does a reconstructed stream actually decode?

It does **not** implement or prove production upload reliability (retries, idempotency,
server ACK semantics, partial sealing, IndexedDB, epoch recovery). The local probe server only
writes chunks to disk and serves them back.

## Automated tests

```
PROBE_QUIET=1 python3 -m unittest tests.cloud.test_probe_upload -v
# Ran 11 tests in 2.206s
# OK
```

The plan names `python -m unittest ...`; this machine has no `python` alias (only `python3`),
so `python3` was used. `PROBE_QUIET=1` only silences the HTTP access log.

Tests (all pass):

- `test_reordered_stream_reconstructs` — three 500 ms PCM chunks fed as 2, 0, 1; result equals
  the original concatenated stream and `pcm_duration_ms` is ~1500 ms.
- `test_ordered_pcm_chunks_are_independent` — PCM chunks need no header.
- `test_container_chunks_are_header_dependent` — WebM magic only in chunk 0; concatenating
  0+1 works, chunk 1 alone raises `HeaderDependencyError` with `dependent=True`.
- `test_single_container_chunk_without_header_is_marked_dependent`.
- `test_missing_sequence_is_rejected` — sequences 0,2 names the missing 1.
- `test_duplicate_sequence_is_rejected`.
- `test_empty_stream_requires_sequence_zero`.
- `test_probe_server_reassembles_out_of_order` — HTTP POST order 2,0,1; status reports
  `[0,1,2]`; reconstruct returns the original bytes.
- `test_probe_server_rejects_missing_sequence` — 409 `{"error":"sequence_gap"}`.
- `test_probe_server_marks_header_dependent_container` — 409
  `{"error":"header_dependency","dependent":true}`.
- `test_probe_server_serves_spike_page` — `GET /` returns 200 HTML.

### Red evidence (before implementation)

The genuine first red run failed on missing modules
(`ModuleNotFoundError: No module named 'spike.cloud_capture.probe_server'`) and the later run
against a signature-only stub failed behaviorally:

```
Ran 11 tests ... FAILED (failures=7, errors=4)
...
AssertionError: b'' != b'\x00\x005\x02i...'
...
SequenceError not raised
HeaderDependencyError not raised
...
NotImplementedError: probe server not implemented
```

The Node suite separately failed with `ERR_MODULE_NOT_FOUND` before `capture.mjs` existed.
Full red/green command transcripts are in the task report.

## Reassembly and header-dependency behaviour

- Accepted input at the library boundary: `[(sequence, bytes), ...]` or `[bytes, ...]`.
- Sequences must start at 0, be unique, and have no gaps; otherwise `SequenceError` names the
  missing sequence(s). Empty input returns `b""` only when the caller explicitly passes an
  empty stream; a container with no header is never silently declared valid.
- Header detection for the probe: WebM/Matroska starts with `1A 45 DF A3`, Ogg with
  `OggS`, MP4/M4A has `ftyp` at bytes 4–8. For a container codec, the first chunk must carry
  the header. If the only chunk lacks it, `HeaderDependencyError` is raised and the HTTP route
  returns 409 with `dependent: true`.
- The probe server's `ChunkStore.put` writes a temp file in the same directory and
  `os.replace`s it into place **before** returning the 200. This is write-before-ACK, but there
  is no `fsync`, no idempotency key, and no retry/ACK ledger — it does not meet §7.2's
  production requirement on its own.

## Real Chrome synthetic evidence (upload path)

Three chunks per track from the 7 s synthetic recording, uploaded in reverse order (2, 1, 0).
See [capture.md](capture.md) for the setup.

| Track | Sizes (seq 0/1/2) | Upload statuses | Status sequences | Reconstruct | SHA-256 match |
|---|---|---|---|---|---|
| mic | 42,666 / 48,316 / 16,437 B | 200, 200, 200 | [0,1,2] | 200, 107,419 B | yes |
| system | 42,666 / 48,316 / 16,437 B | 200, 200, 200 | [0,1,2] | 200, 107,419 B | yes |

Reconstructed hashes:

- mic: `a8185c80c73015e3dcbbf85a8beb02078e6b880f0d301dd1c7b157bb137b4903`
- system: `995380f8a5d7289d1260f7f861db4ac4cef443e0592209d564c8f8263fd5b7c5`

Uploading chunk 1 alone and asking the server to reconstruct it returned:

```json
{
  "error": "header_dependency",
  "dependent": true,
  "message": "first chunk is not a audio/webm;codecs=opus header; this blob is dependent on an earlier header chunk and cannot be decoded alone"
}
```

This is the concrete observation behind `continuity_mode = "header+ordered_chunks"`: the first
chunk carries the WebM header; later chunks are not standalone files.

## Decode evidence

Homebrew ffmpeg is broken on this machine, so **no ffmpeg was used**:

```
$ /opt/homebrew/bin/ffmpeg -version
dyld[...]: Library not loaded: /opt/homebrew/opt/lame/lib/libmp3lame.0.dylib
  Referenced from: .../Cellar/ffmpeg/8.0.1_4/bin/ffmpeg
  Reason: tried: '/opt/homebrew/opt/lame/lib/libmp3lame.0.dylib' (no such file)
Abort trap: 6
# exit 134
```

Rather than fake an ffmpeg pass, the reconstructed bytes were decoded by **Chrome itself**
(`Audio` element with the Blob) inside the same Playwright session:

- full reconstructed stream: `error = null`, `duration = 6.6 s`, `canplay = true`,
  `played_seconds = 0.590301` after ~700 ms;
- chunk 1 alone: `4: PipelineStatus::DEMUXER_ERROR_COULD_NOT_OPEN: FFmpegDemuxer: open context
  failed`, `duration = null`.

No test was marked "skipped" for ffmpeg; the browser-native decode is an honest, independent
decode path. The AudioWorklet PCM comparison mentioned in Step 3 was not run, because the
browser decode path was available and the probe did not measure a real-time latency target.

## Not verified here

- 30 s network loss, retry, duplicate upload over HTTP, or server dedup by hash.
- Refresh / crash / sleep recovery, epoch re-anchoring, or tail-chunk handling.
- Share-stop mid-recording and the two-track independent-stop behaviour.
- Long session (2 h), local buffer limits, save-lag targets, or throughput.
- Real-time decode latency / real-time transcription feasibility.
- A real object store, durable ACK after crash, or partial sealing.
- Anything under `AC07`–`AC09` beyond out-of-order local reassembly.

All of these remain `not_run` in `capture-contract.json`; none is claimed as passing.

## Contract conclusion

Measured for Chrome 157 + synthetic streams:

```
selected_codec        = "audio/webm;codecs=opus"
chunk_target_ms       = 3000
continuity_mode       = "header+ordered_chunks"
video_track_lifecycle = "kept_live_unencoded"
```

These are probe findings, not a production interface or a compatibility promise.
