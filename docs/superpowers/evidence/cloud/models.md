# Cloud model probe evidence (Task 3)

Status: **not measured**. This file records a probe harness, a contract validator
and the proposed merge rules. It contains no CER, speaker error, latency, memory
or capacity number, because no Linux measurement host, model weights, licensed
corpus or CUDA device existed in this worktree.

- Plan: `docs/superpowers/plans/2026-11-14-cloud-meeting-recorder.md` Task 3
- Design: `docs/product/cloud-meeting-recorder-design.md` §8 (ASR), §9 (voiceprint), §16.2 (probe 3)
- Harness: `spike/cloud_models.py`
- Tests: `tests/cloud/test_model_probe.py`
- Contract: `docs/superpowers/evidence/cloud/model-contract.json`

## What the harness does

`evaluate_models(dataset, output_dir, *, host=None, engine=None, ...)`:

1. Probes the host: OS, machine, Python, ffmpeg path/version, CUDA/ROCm device,
   weights directory (file names, sizes, SHA-256), installed package versions.
2. Checks the labelled dataset directory, weights, ffmpeg and Linux compute
   device. If any is missing, it returns `status="not_measured"` with a
   `missing_prerequisites` list naming the exact gap and writes
   `model-probe-result.json` to `output_dir`.
3. If the environment is complete but no engine adapter is supplied, it still
   returns `not_measured` with `prerequisite="measurement_engine"`.
4. Only an injected `engine(dataset)` that returns both strategy measurements
   can produce `status="measured"`. The result keeps
   `recognition.strategy="proposed_pending_measurement"`; measurement is not
   approval. Engine crashes and incomplete engine results become
   `status="measurement_failed"` with `engine_error`, never fake metrics.

The CLI targets the real Linux host:

```bash
python3 spike/cloud_models.py --dataset /data/authorised-corpus --out /data/probe \
  --weights /models --engine my_engine:run
```

`CLOUD_MODEL_DEVICE`, `CLOUD_MODEL_WEIGHTS_DIR` and `CLOUD_MODEL_FFMPEG`
override auto-detection.

## Contract validation

`validate_model_contract(contract)` raises `ContractError` when a strategy is
`approved` without all of:

- measured numeric metrics (a `not_measured` status marker is not a metric),
- corpus id/description/positive utterance count,
- exact package versions,
- valid SHA-256 weight checksums,
- license records with a name,
- a calibration file plus SHA-256,
- a named `approved_strategy` (`mixed` or `separate_merge`).

A voiceprint compatibility claim is rejected when the full model metadata does
not match. In particular, equal embedding dimension with a different weight
digest is **not compatible**: the digest is compared explicitly, and the error
message states that a dimension match is never sufficient.

## Mixed vs separate_merge comparison plan

| Strategy | Definition |
| --- | --- |
| `mixed` | Unified recognition over the mixed meeting audio; track labels are used only for attribution afterwards. |
| `separate_merge` | Per-track recognition, then cross-track dedup and speaker clustering into stable meeting identities. |

Both must report CER, speaker error rate, false accept / false reject, latency,
peak memory and stream capacity on the same authorised corpus (Chinese-only,
Chinese/English, silence, overlap, cross-track same person, multi-remote).
Comparison deltas are `separate_merge - mixed`. The contract gate remains
`pending_user_approval`; the code never selects a winner.

## Cross-track identity fixtures

The fixtures use an injected synthetic scorer. They prove merge logic, not model
quality, and need no weights or audio.

| Fixture | Expected identities | Expected utterances | Expected duplicates | Why |
| --- | --- | --- | --- | --- |
| `same_person_across_tracks` | 1 | 2 | 0 | Same person speaks on mic and system; one stable identity, two different sentences. |
| `duplicate_pickup` | 1 | 1 | 1 | System playback is recaptured by the mic; concise dedup keeps one utterance. |
| `two_remote_speakers` | 2 | 2 | 0 | Two different remote people share the system track; a source label is not an identity. |

The duplicate rule requires different tracks, overlapping time, a start within
the dedup window and near-identical normalised text. The cluster rule is greedy
cosine matching against the running centroid. Both are exercised with synthetic
vectors; real embeddings are Step 4 work.

## Proposed, uncalibrated parameters

| Parameter | Proposed value | Rationale |
| --- | --- | --- |
| `dedup_window_ms` | 1200 | Covers capture/scheduling offset between playback and mic bleed without merging a deliberate repeat later in the meeting. |
| `dedup_min_text_similarity` | 0.90 | Near-identical transcript required; tolerates ASR punctuation drift, rejects a different sentence. |
| `cluster_similarity_threshold` | 0.75 | Conservative cosine start for one embedding model; must be recalibrated per model. Deliberately not the old 0.5/0.35 voiceprint threshold. |
| `max_identities` | 20 | Matches the design speaker-hint ceiling; a guardrail, not a merge rule. |

All are marked `status="proposed"` in the contract. None may be presented as
calibrated.

## Step 4 measurements: all not_run

| Measurement | Status | Why |
| --- | --- | --- |
| Linux environment | not_run | Host is macOS arm64; no Linux host with CUDA/ROCm. |
| Offline cold start | not_run | No weights or model cache. |
| Chinese CER | not_run | No authorised corpus, no ASR weights. |
| Mixed Chinese/English CER | not_run | No authorised corpus, no ASR weights. |
| Silence / hallucination | not_run | No silence corpus, no model. |
| Overlap speaker error | not_run | No overlap corpus, no diarization weights. |
| Cross-track same person | not_run | Synthetic scorer only. |
| Multi-remote speakers | not_run | No real recording or embeddings. |
| False accept / false reject | not_run | No owner library, no impostor trials. |
| Latency / memory / capacity | not_run | No model process on a Linux host. |
| Mixed vs separate_merge | not_run | Both need the same corpus and weights. |
| Voiceprint template compatibility | not_run | No second model or digest to compare. |

## TDD evidence

RED (Step 2, abridged; the file then held only the three named tests, the stub
validator raised nothing and the stub fixtures returned zero counts):

```
$ python3 -m unittest tests.cloud.test_model_probe -v
test_contract_rejects_unmeasured_strategy ... FAIL
AssertionError: ContractError not raised
test_cross_track_identity_fixture ... FAIL
AssertionError: 0 != 1
test_same_dimension_not_compatible ... FAIL
AssertionError: True is not false
FAILED (failures=3)
```

GREEN is the recorded run in the task report; the committed contract keeps every
measured field empty/null and `recognition.strategy=proposed_pending_measurement`.
