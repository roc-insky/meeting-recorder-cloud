"""Linux model probe and cross-track identity contract for the cloud recorder.

This spike answers two questions Task 3 has to settle before Task 10 can pick a
recognition strategy:

* Can a recognition strategy be approved?  Only when a real measurement on the
  target Linux host produced metrics, corpus metadata, exact package versions,
  weight checksums, license records and a calibration file.  evaluate_models
  never invents a metric: when the dataset, weights, ffmpeg, Linux compute
  device or measurement engine is missing it returns a structured
  not_measured result and writes the exact missing prerequisites.
* How are the same person, a duplicate pickup and two remote speakers handled
  across tracks?  evaluate_identity_fixture runs the proposed dedup and
  clustering rules against an injected scorer.  The fixtures use synthetic
  vectors, so they prove the merge logic only - they are not model quality.

Nothing here is a production API.  No weights, audio, or network access are
bundled.  The proposed parameters are deliberately marked proposed; none of
them is calibrated.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

CONTRACT_VERSION = "0.1.0"
TASK_REFERENCE = (
    "docs/superpowers/plans/2026-11-14-cloud-meeting-recorder.md Task 3"
)
PROBE_RESULT_FILE = "model-probe-result.json"

UNMEASURED_STRATEGY = "proposed_pending_measurement"
APPROVED_STRATEGY = "approved"
ALLOWED_STRATEGY_STATES = (UNMEASURED_STRATEGY, APPROVED_STRATEGY)
RECOGNITION_STRATEGIES = ("mixed", "separate_merge")

EVIDENCE_FIELDS = (
    "metrics",
    "corpus",
    "package_versions",
    "weights_sha256",
    "license",
    "calibration",
)

MODEL_METADATA_FIELDS = (
    "model_id",
    "model_version",
    "weights_sha256",
    "dimension",
    "sample_rate",
    "preprocess_version",
    "normalization_version",
    "calibration_version",
)

# Proposed, uncalibrated merge parameters.  Task 3 Step 3 records the values and
# the reasoning; Step 4 must measure them on the approved corpus before any of
# them can become a locked contract value.
PROPOSED_DEDUP_WINDOW_MS = 1200
PROPOSED_DEDUP_MIN_TEXT_SIMILARITY = 0.90
PROPOSED_CLUSTER_SIMILARITY_THRESHOLD = 0.75
PROPOSED_MAX_IDENTITIES = 20

FIXTURE_EXPECTATIONS = {
    "same_person_across_tracks": {
        "identity_count": 1,
        "utterance_count": 2,
        "duplicate_count": 0,
    },
    "duplicate_pickup": {
        "identity_count": 1,
        "utterance_count": 1,
        "duplicate_count": 1,
    },
    "two_remote_speakers": {
        "identity_count": 2,
        "utterance_count": 2,
        "duplicate_count": 0,
    },
}

_COMPUTE_DEVICE_ENV = "CLOUD_MODEL_DEVICE"
_WEIGHTS_ENV = "CLOUD_MODEL_WEIGHTS_DIR"
_FFMPEG_ENV = "CLOUD_MODEL_FFMPEG"
_CANDIDATE_PACKAGES = (
    "onnxruntime",
    "onnxruntime-gpu",
    "faster-whisper",
    "ctranslate2",
    "torch",
    "torchaudio",
    "numpy",
    "soundfile",
    "librosa",
    "pyannote.audio",
)


class ContractError(ValueError):
    """The model contract makes a claim that measured evidence does not support."""


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _license_ok(value: object) -> bool:
    if _nonempty_text(value):
        return True
    if isinstance(value, dict):
        return _nonempty_text(value.get("name")) or _nonempty_text(
            value.get("license")
        )
    return False


def _approval_gaps(evidence: object) -> List[str]:
    """Return every reason a strategy cannot be approved from this evidence."""
    if not isinstance(evidence, dict):
        return [
            "evidence: missing measured evidence entries ("
            + ", ".join(EVIDENCE_FIELDS)
            + ")"
        ]

    gaps: List[str] = []

    metrics = evidence.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        gaps.append("metrics: missing measured metrics")
    elif not any(_is_number(value) for value in metrics.values()):
        gaps.append(
            "metrics: no finite numeric values; a status marker is not a metric"
        )

    corpus = evidence.get("corpus")
    if not isinstance(corpus, dict) or not corpus:
        gaps.append("corpus: missing corpus description")
    else:
        if not _nonempty_text(corpus.get("id")):
            gaps.append("corpus: id must be a non-empty string")
        if not _nonempty_text(corpus.get("description")):
            gaps.append("corpus: description must be a non-empty string")
        count = corpus.get("utterance_count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            gaps.append("corpus: utterance_count must be a positive integer")

    versions = evidence.get("package_versions")
    if (
        not isinstance(versions, dict)
        or not versions
        or not all(
            _nonempty_text(name) and _nonempty_text(version)
            for name, version in versions.items()
        )
    ):
        gaps.append("package_versions: exact package versions required")

    digests = evidence.get("weights_sha256")
    if not isinstance(digests, dict) or not digests:
        gaps.append("weights_sha256: weight checksums required")
    else:
        invalid = sorted(name for name, value in digests.items() if not _is_sha256(value))
        if invalid:
            gaps.append(
                "weights_sha256: invalid sha256 digest for " + ", ".join(invalid)
            )

    licenses = evidence.get("license")
    if not isinstance(licenses, dict) or not licenses:
        gaps.append("license: license evidence required")
    else:
        invalid = sorted(
            name for name, value in licenses.items() if not _license_ok(value)
        )
        if invalid:
            gaps.append("license: missing license name for " + ", ".join(invalid))

    calibration = evidence.get("calibration")
    if not isinstance(calibration, dict) or not calibration:
        gaps.append("calibration: calibration file required")
    else:
        if not _nonempty_text(calibration.get("file")):
            gaps.append("calibration: file path required")
        if not _is_sha256(calibration.get("sha256")):
            gaps.append("calibration: sha256 digest required")

    return gaps


def voiceprint_compatible(left: object, right: object) -> bool:
    """True only when full ModelMetadata matches, digest included.

    Equal embedding dimension is never sufficient: the current local 512-dim
    templates cannot be reused for another model.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False

    for field in MODEL_METADATA_FIELDS:
        left_value = left.get(field)
        right_value = right.get(field)

        if field == "weights_sha256":
            if not _is_sha256(left_value) or not _is_sha256(right_value):
                return False
            if left_value.lower() != right_value.lower():
                return False
        elif field in ("dimension", "sample_rate"):
            if (
                isinstance(left_value, bool)
                or not isinstance(left_value, int)
                or left_value <= 0
            ):
                return False
            if left_value != right_value:
                return False
        else:
            if not _nonempty_text(left_value) or left_value != right_value:
                return False

    return True


def _incompatibility_reasons(left: dict, right: dict) -> List[str]:
    reasons: List[str] = []

    left_digest = left.get("weights_sha256")
    right_digest = right.get("weights_sha256")
    if not _is_sha256(left_digest):
        reasons.append("weights_sha256: left model digest is missing or invalid")
    elif not _is_sha256(right_digest):
        reasons.append("weights_sha256: right model digest is missing or invalid")
    elif left_digest.lower() != right_digest.lower():
        dimension = left.get("dimension")
        if dimension == right.get("dimension"):
            dimension_note = "dimension %s matches" % (dimension,)
        else:
            dimension_note = "dimension %s != %s" % (
                dimension,
                right.get("dimension"),
            )
        reasons.append(
            "weights_sha256: model weight digest differs (%s... != %s...); %s, "
            "but a dimension match is never sufficient"
            % (left_digest[:12], right_digest[:12], dimension_note)
        )

    for field in MODEL_METADATA_FIELDS:
        if field == "weights_sha256":
            continue
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value != right_value:
            reasons.append("%s: differs or missing" % (field,))
        elif field in ("dimension", "sample_rate"):
            if (
                isinstance(left_value, bool)
                or not isinstance(left_value, int)
                or left_value <= 0
            ):
                reasons.append("%s: invalid value" % (field,))
        elif not _nonempty_text(left_value):
            reasons.append("%s: missing" % (field,))

    return reasons


def _validate_voiceprint_claim(contract: dict) -> None:
    section = contract.get("voiceprint_compatibility")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ContractError("voiceprint_compatibility must be a JSON object")

    claim = section.get("claim")
    if claim is None:
        return
    if not isinstance(claim, dict):
        raise ContractError("voiceprint_compatibility.claim must be a JSON object")

    compatible = claim.get("compatible")
    if compatible is False:
        return
    if compatible is not True:
        raise ContractError(
            "voiceprint_compatibility.claim.compatible must be true or false"
        )

    left = claim.get("left")
    right = claim.get("right")
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ContractError(
            "voiceprint compatibility claimed without both ModelMetadata records; "
            "the weight digest cannot be compared"
        )

    if not voiceprint_compatible(left, right):
        raise ContractError(
            "voiceprint compatibility claim rejected: "
            + "; ".join(_incompatibility_reasons(left, right))
            + ". Embedding dimension alone never proves compatibility."
        )


def validate_model_contract(contract: dict) -> None:
    """Raise ContractError when the contract overstates its evidence.

    An approved recognition strategy requires all of: measured metrics, a
    corpus description, exact package versions, weight checksums, license
    records, a calibration file and the name of the approved strategy.  A
    voiceprint compatibility claim is rejected when the model weight digest
    differs, even if the embedding dimensions match.
    """
    if not isinstance(contract, dict):
        raise ContractError("model contract must be a JSON object")

    if "recognition" in contract:
        recognition = contract.get("recognition")
        if not isinstance(recognition, dict):
            raise ContractError("recognition must be a JSON object")
        strategy = recognition.get("strategy")
        evidence = recognition.get("evidence")
        approved_strategy = recognition.get("approved_strategy")
    else:
        strategy = contract.get("recognition_strategy")
        evidence = contract.get("evidence")
        approved_strategy = contract.get("approved_strategy")

    if strategy not in ALLOWED_STRATEGY_STATES:
        raise ContractError(
            "recognition strategy must be one of %s, got %r"
            % (", ".join(ALLOWED_STRATEGY_STATES), strategy)
        )

    if strategy == APPROVED_STRATEGY:
        gaps = _approval_gaps(evidence)
        if approved_strategy not in RECOGNITION_STRATEGIES:
            gaps.append(
                "approved_strategy: must name one of "
                + ", ".join(RECOGNITION_STRATEGIES)
            )
        if gaps:
            raise ContractError(
                "model contract cannot approve a recognition strategy without "
                "measured evidence; missing or invalid: "
                + "; ".join(gaps)
                + ". A not_measured status is not evidence."
            )

    _validate_voiceprint_claim(contract)


# ---------------------------------------------------------------------------
# Host / prerequisite probe
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ffmpeg_version(ffmpeg_path: Optional[str]) -> Optional[str]:
    if not ffmpeg_path:
        return None
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    first_line = completed.stdout.decode("utf-8", "replace").splitlines()
    return first_line[0].strip() if first_line else None


def _detect_compute_device(system: str) -> Optional[str]:
    if system != "Linux":
        return None
    if Path("/dev/nvidia0").exists():
        return "cuda"
    if Path("/dev/kfd").exists():
        return "rocm"
    try:
        render_nodes = sorted(Path("/dev/dri").glob("renderD*"))
    except OSError:
        render_nodes = []
    if render_nodes:
        return "dri"
    return None


def _distribution_installed(name: str) -> bool:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    except Exception:
        return False
    return True


def _installed_package_versions() -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for name in _CANDIDATE_PACKAGES:
        if not _distribution_installed(name):
            continue
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
        versions[name] = version
    return versions


def _weight_entries(root: Path) -> List[dict]:
    if not root.is_dir():
        return []
    entries: List[dict] = []
    try:
        files = sorted(path for path in root.rglob("*") if path.is_file())
    except OSError:
        return []
    for path in files:
        try:
            size = path.stat().st_size
            digest = _sha256_file(path)
            name = str(path.relative_to(root))
        except OSError:
            continue
        entries.append({"name": name, "bytes": size, "sha256": digest})
    return entries


def probe_host(
    ffmpeg: Optional[str] = None,
    device: Optional[str] = None,
    weights_dir: Optional[str] = None,
) -> dict:
    """Observe the measurement host.  No model is imported and no metric made."""
    system = platform.system() or ""
    ffmpeg_path = ffmpeg or os.environ.get(_FFMPEG_ENV) or shutil.which("ffmpeg")
    compute_device = (
        device
        or os.environ.get(_COMPUTE_DEVICE_ENV)
        or _detect_compute_device(system)
    )
    selected_weights = weights_dir or os.environ.get(_WEIGHTS_ENV)
    weights = _weight_entries(Path(selected_weights)) if selected_weights else []
    return {
        "system": system,
        "machine": platform.machine() or "",
        "python_version": platform.python_version(),
        "ffmpeg_path": ffmpeg_path,
        "ffmpeg_version": _ffmpeg_version(ffmpeg_path),
        "compute_device": compute_device,
        "weights_dir": selected_weights,
        "weights": weights,
        "package_versions": _installed_package_versions(),
    }


def _dataset_gap(dataset: Path) -> Optional[dict]:
    if not dataset.is_dir():
        return {
            "prerequisite": "dataset",
            "detail": "labelled dataset directory not found",
            "expected": str(dataset),
        }
    try:
        files = [
            path
            for path in dataset.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        ]
    except OSError:
        files = []
    if not files:
        return {
            "prerequisite": "dataset",
            "detail": "dataset directory contains no labelled corpus files",
            "expected": str(dataset),
        }
    return None


def _host_gaps(host: dict) -> List[dict]:
    gaps: List[dict] = []

    system = host.get("system") or ""
    if system != "Linux":
        gaps.append(
            {
                "prerequisite": "linux_device",
                "detail": (
                    "target measurement host must be Linux with a visible "
                    "CUDA/ROCm device; this host reports %r" % (system or "unknown")
                ),
                "expected": "Linux",
            }
        )
    elif not host.get("compute_device"):
        gaps.append(
            {
                "prerequisite": "linux_device",
                "detail": "Linux host has no detected CUDA/ROCm device",
                "expected": "CUDA or ROCm device (or set %s)" % (_COMPUTE_DEVICE_ENV,),
            }
        )

    if not host.get("weights_dir") or not host.get("weights"):
        gaps.append(
            {
                "prerequisite": "weights",
                "detail": (
                    "model weights directory is missing or empty; set %s to the "
                    "directory holding ASR/diarization/embedding weights"
                )
                % (_WEIGHTS_ENV,),
                "expected": "weights with sha256 checksums",
            }
        )

    if not host.get("ffmpeg_path") or not host.get("ffmpeg_version"):
        gaps.append(
            {
                "prerequisite": "ffmpeg",
                "detail": "ffmpeg executable or version is unavailable",
                "expected": "ffmpeg on PATH or %s" % (_FFMPEG_ENV,),
            }
        )

    return gaps


# ---------------------------------------------------------------------------
# Measurement entry point
# ---------------------------------------------------------------------------


def _engine_error(measured: object) -> Optional[str]:
    if not isinstance(measured, dict):
        return "engine result must be a JSON object"
    strategies = measured.get("strategies")
    if not isinstance(strategies, dict):
        return "engine result must contain a 'strategies' mapping"
    missing = [
        name for name in RECOGNITION_STRATEGIES if name not in strategies
    ]
    if missing:
        return (
            "engine must report both recognition strategies; missing: "
            + ", ".join(missing)
        )
    for name in RECOGNITION_STRATEGIES:
        payload = strategies.get(name)
        if not isinstance(payload, dict):
            return "strategy %r must be a JSON object" % (name,)
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            return "strategy %r has no metrics" % (name,)
        if not any(_is_number(value) for value in metrics.values()):
            return "strategy %r metrics contain no finite numeric values" % (name,)
    return None


def _compare_strategies(strategies: dict) -> dict:
    mixed_metrics = strategies["mixed"]["metrics"]
    separate_metrics = strategies["separate_merge"]["metrics"]
    deltas: Dict[str, float] = {}
    for name in sorted(set(mixed_metrics) & set(separate_metrics)):
        mixed = mixed_metrics[name]
        separate = separate_metrics[name]
        if _is_number(mixed) and _is_number(separate):
            deltas[name] = round(float(separate) - float(mixed), 6)
    return {
        "basis": (
            "separate_merge minus mixed; a negative delta means separate_merge "
            "reported the lower value"
        ),
        "metric_deltas": deltas,
        "decision": "pending_user_approved_gate",
        "decision_reason": (
            "No user-approved corpus/quality gate is recorded; the probe reports "
            "both strategies for review and does not lock one."
        ),
    }


def _write_result(output_dir: Path, result: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / PROBE_RESULT_FILE
    target.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def evaluate_models(
    dataset: Path,
    output_dir: Path,
    *,
    host: Optional[dict] = None,
    engine: Optional[Callable[[Path], dict]] = None,
    weights_dir: Optional[str] = None,
    ffmpeg: Optional[str] = None,
    device: Optional[str] = None,
) -> dict:
    """Measure ASR/diarization/embedding quality, or say why that is impossible.

    engine is the target-host adapter that actually runs the models and
    returns {"strategies": {"mixed": {"metrics": {...}}, "separate_merge":
    {"metrics": {...}}}, ...}.  Without a real engine no metric is recorded.
    The result is always written to output_dir/model-probe-result.json.
    """
    dataset_path = Path(dataset)
    output_path = Path(output_dir)

    if host is not None:
        if not isinstance(host, dict):
            raise TypeError("host must be a mapping of observed host facts")
        facts = dict(host)
    else:
        facts = probe_host(ffmpeg=ffmpeg, device=device, weights_dir=weights_dir)

    result: dict = {
        "contract_version": CONTRACT_VERSION,
        "task": TASK_REFERENCE,
        "status": "not_measured",
        "generated_at": _utc_now(),
        "dataset": str(dataset_path),
        "output_dir": str(output_path),
        "environment": facts,
        "missing_prerequisites": [],
        "measurements": {},
        "recognition": {
            "strategy": UNMEASURED_STRATEGY,
            "approved_strategy": None,
            "evidence_status": "not_measured",
            "decision": "pending_user_approved_gate",
            "decision_reason": (
                "A measurement alone does not approve a strategy; approval also "
                "needs the user-approved corpus gate and a reviewed contract."
            ),
            "comparison": None,
        },
        "identity_fixture_expectations": FIXTURE_EXPECTATIONS,
        "notes": [
            "No metric is recorded unless an injected engine computed it on this host.",
            "This probe never sets recognition.strategy to approved.",
            "Synthetic identity fixtures prove merge logic only, not model quality.",
        ],
    }

    gaps: List[dict] = []
    dataset_problem = _dataset_gap(dataset_path)
    if dataset_problem is not None:
        gaps.append(dataset_problem)
    gaps.extend(_host_gaps(facts))

    if gaps:
        result["missing_prerequisites"] = gaps
        result["notes"].append(
            "No measurement run: prerequisites are missing. This file is a "
            "prerequisite report, not a quality result."
        )
        _write_result(output_path, result)
        return result

    if engine is None:
        result["missing_prerequisites"] = [
            {
                "prerequisite": "measurement_engine",
                "detail": (
                    "no engine adapter was supplied; a real ASR/diarization/"
                    "embedding run is required before any metric can be recorded"
                ),
                "expected": "engine callable returning both strategy measurements",
            }
        ]
        result["notes"].append(
            "No engine adapter: the probe refuses to copy numbers from a document "
            "or configuration file."
        )
        _write_result(output_path, result)
        return result

    if not callable(engine):
        raise TypeError("engine must be callable")

    try:
        measured = engine(dataset_path)
    except Exception as exc:  # noqa: BLE001 - recorded, never turned into metrics
        result["status"] = "measurement_failed"
        result["engine_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        _write_result(output_path, result)
        return result

    engine_problem = _engine_error(measured)
    if engine_problem is not None:
        result["status"] = "measurement_failed"
        result["engine_error"] = {
            "type": "InvalidEngineResult",
            "message": engine_problem,
        }
        _write_result(output_path, result)
        return result

    result["status"] = "measured"
    result["measurements"] = measured
    result["recognition"]["evidence_status"] = "measured_pending_review"
    result["recognition"]["comparison"] = _compare_strategies(
        measured["strategies"]
    )
    result["notes"].append(
        "Measured output is a candidate for the contract, not an approval. "
        "Promote it only after review and validate_model_contract()."
    )
    _write_result(output_path, result)
    return result


# ---------------------------------------------------------------------------
# Cross-track identity fixtures (synthetic; injectable scorer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackUtterance:
    utterance_id: str
    track_id: str
    start_ms: int
    end_ms: int
    text: str
    voice_key: str


@dataclass(frozen=True)
class CrossTrackFixture:
    name: str
    description: str
    utterances: Tuple[TrackUtterance, ...]
    expected_identity_count: int
    expected_utterance_count: int
    expected_duplicate_count: int


@dataclass(frozen=True)
class Identity:
    identity_id: str
    utterance_ids: Tuple[str, ...]
    track_ids: Tuple[str, ...]


@dataclass(frozen=True)
class IdentityEvaluation:
    fixture_name: str
    identities: Tuple[Identity, ...]
    utterance_count: int
    duplicate_count: int
    dropped_utterance_ids: Tuple[str, ...]
    raw_utterance_count: int
    scorer_calls: int
    params: dict

    @property
    def identity_count(self) -> int:
        return len(self.identities)


def same_person_across_tracks_fixture() -> CrossTrackFixture:
    return CrossTrackFixture(
        name="same_person_across_tracks",
        description=(
            "The same person speaks once on the mic track and once on the system "
            "track; one stable meeting identity is expected."
        ),
        utterances=(
            TrackUtterance(
                "mic-1",
                "mic",
                0,
                2400,
                "我们先把今天的三项议程过一遍。",
                "alice",
            ),
            TrackUtterance(
                "system-1",
                "system",
                2600,
                5200,
                "第二项的数据我稍后补到文档里。",
                "alice",
            ),
        ),
        expected_identity_count=1,
        expected_utterance_count=2,
        expected_duplicate_count=0,
    )


def duplicate_pickup_fixture() -> CrossTrackFixture:
    return CrossTrackFixture(
        name="duplicate_pickup",
        description=(
            "Computer playback is captured by the system track and picked up "
            "again by the microphone; one utterance is expected."
        ),
        utterances=(
            TrackUtterance(
                "system-1",
                "system",
                0,
                2600,
                "这一句来自电脑播放的声音。",
                "bob",
            ),
            TrackUtterance(
                "mic-1",
                "mic",
                80,
                2680,
                "这一句来自电脑播放的声音。",
                "bob",
            ),
        ),
        expected_identity_count=1,
        expected_utterance_count=1,
        expected_duplicate_count=1,
    )


def two_remote_speakers_fixture() -> CrossTrackFixture:
    return CrossTrackFixture(
        name="two_remote_speakers",
        description=(
            "Two different remote people speak on the same system track; two "
            "identities are expected, so a source label must never be an identity."
        ),
        utterances=(
            TrackUtterance(
                "system-1",
                "system",
                0,
                2600,
                "第一位远端同事先介绍项目背景。",
                "carol",
            ),
            TrackUtterance(
                "system-2",
                "system",
                2800,
                5400,
                "第二位远端同事补充验收结论。",
                "dave",
            ),
        ),
        expected_identity_count=2,
        expected_utterance_count=2,
        expected_duplicate_count=0,
    )


def _normalise_text(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def _text_similarity(left: str, right: str) -> float:
    left_normalised = _normalise_text(left)
    right_normalised = _normalise_text(right)
    if not left_normalised or not right_normalised:
        return 0.0
    if left_normalised == right_normalised:
        return 1.0
    return difflib.SequenceMatcher(
        None, left_normalised, right_normalised
    ).ratio()


def _overlap_ms(left: TrackUtterance, right: TrackUtterance) -> int:
    return min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms)


def _as_embedding(value: object, utterance_id: str) -> Tuple[float, ...]:
    try:
        vector = tuple(float(component) for component in value)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        raise ValueError(
            "scorer must return a numeric embedding for %r" % (utterance_id,)
        )
    if not vector:
        raise ValueError("scorer returned an empty embedding for %r" % (utterance_id,))
    if not all(math.isfinite(component) for component in vector):
        raise ValueError(
            "scorer returned a non-finite embedding for %r" % (utterance_id,)
        )
    return vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = math.sqrt(sum(component * component for component in left))
    right_norm = math.sqrt(sum(component * component for component in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    return dot / (left_norm * right_norm)


def _mean_vector(vectors: Sequence[Sequence[float]]) -> Tuple[float, ...]:
    count = len(vectors)
    return tuple(
        sum(vector[index] for vector in vectors) / count
        for index in range(len(vectors[0]))
    )


def evaluate_identity_fixture(
    fixture: CrossTrackFixture,
    scorer: Callable[[TrackUtterance], Sequence[float]],
    *,
    dedup_window_ms: int = PROPOSED_DEDUP_WINDOW_MS,
    dedup_min_text_similarity: float = PROPOSED_DEDUP_MIN_TEXT_SIMILARITY,
    cluster_similarity_threshold: float = PROPOSED_CLUSTER_SIMILARITY_THRESHOLD,
) -> IdentityEvaluation:
    """Apply the proposed dedup and clustering rules to a synthetic fixture.

    scorer is injected: the probe never loads a model implicitly, and the
    fixture vectors are not model quality evidence.
    """
    if not isinstance(fixture, CrossTrackFixture):
        raise TypeError("fixture must be a CrossTrackFixture")
    if not callable(scorer):
        raise TypeError(
            "an injected scorer callable is required; this probe never loads a "
            "model implicitly"
        )
    if dedup_window_ms < 0:
        raise ValueError("dedup_window_ms must be >= 0")
    if not 0.0 <= dedup_min_text_similarity <= 1.0:
        raise ValueError("dedup_min_text_similarity must be within [0, 1]")
    if not 0.0 <= cluster_similarity_threshold <= 1.0:
        raise ValueError("cluster_similarity_threshold must be within [0, 1]")

    ordered = sorted(
        fixture.utterances,
        key=lambda utterance: (
            utterance.start_ms,
            utterance.track_id,
            utterance.utterance_id,
        ),
    )

    scored: List[dict] = []
    scorer_calls = 0
    dimension: Optional[int] = None
    for utterance in ordered:
        if utterance.end_ms <= utterance.start_ms:
            raise ValueError(
                "utterance %r must end after it starts"
                % (utterance.utterance_id,)
            )
        vector = _as_embedding(scorer(utterance), utterance.utterance_id)
        scorer_calls += 1
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError(
                "scorer returned inconsistent embedding dimensions: %d vs %d"
                % (dimension, len(vector))
            )
        scored.append({"utterance": utterance, "vector": vector})

    kept: List[dict] = []
    dropped: List[str] = []
    for entry in scored:
        duplicate_of: Optional[dict] = None
        current = entry["utterance"]
        for existing in kept:
            candidate = existing["utterance"]
            if candidate.track_id == current.track_id:
                continue
            if (
                _text_similarity(candidate.text, current.text)
                < dedup_min_text_similarity
            ):
                continue
            if abs(candidate.start_ms - current.start_ms) > dedup_window_ms:
                continue
            if _overlap_ms(candidate, current) <= 0:
                continue
            duplicate_of = existing
            break

        if duplicate_of is not None:
            dropped.append(current.utterance_id)
            # Prefer the digital source over microphone bleed when both captured
            # the same utterance, so the merged text keeps the cleaner timeline.
            if (
                duplicate_of["utterance"].track_id != "system"
                and current.track_id == "system"
            ):
                duplicate_of["utterance"] = current
                duplicate_of["vector"] = entry["vector"]
            continue

        kept.append(entry)

    clusters: List[dict] = []
    for entry in kept:
        vector = entry["vector"]
        best_index: Optional[int] = None
        best_similarity = -1.0
        for index, cluster in enumerate(clusters):
            similarity = _cosine(vector, cluster["centroid"])
            if similarity > best_similarity:
                best_similarity = similarity
                best_index = index
        if best_index is not None and best_similarity >= cluster_similarity_threshold:
            cluster = clusters[best_index]
            cluster["members"].append(entry)
            cluster["centroid"] = _mean_vector(
                [member["vector"] for member in cluster["members"]]
            )
        else:
            clusters.append({"members": [entry], "centroid": vector})

    identities: List[Identity] = []
    for index, cluster in enumerate(clusters, start=1):
        members = cluster["members"]
        identities.append(
            Identity(
                identity_id="spk_%d" % (index,),
                utterance_ids=tuple(
                    member["utterance"].utterance_id for member in members
                ),
                track_ids=tuple(
                    sorted(
                        {
                            member["utterance"].track_id
                            for member in members
                        }
                    )
                ),
            )
        )

    return IdentityEvaluation(
        fixture_name=fixture.name,
        identities=tuple(identities),
        utterance_count=len(kept),
        duplicate_count=len(dropped),
        dropped_utterance_ids=tuple(dropped),
        raw_utterance_count=len(fixture.utterances),
        scorer_calls=scorer_calls,
        params={
            "dedup_window_ms": dedup_window_ms,
            "dedup_min_text_similarity": dedup_min_text_similarity,
            "cluster_similarity_threshold": cluster_similarity_threshold,
            "max_identities": PROPOSED_MAX_IDENTITIES,
            "parameter_status": "proposed_not_measured",
        },
    )


# ---------------------------------------------------------------------------
# CLI for the target Linux host
# ---------------------------------------------------------------------------


def _load_engine(spec: str) -> Callable[[Path], dict]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("engine must be specified as 'module:callable'")
    module = importlib.import_module(module_name)
    engine = getattr(module, attribute)
    if not callable(engine):
        raise TypeError("engine %r is not callable" % (spec,))
    return engine


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Linux ASR/diarization/embedding models on a labelled corpus. "
            "Without the real environment this reports not_measured."
        )
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--weights", default=None)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument(
        "--engine",
        default=None,
        help="module:callable that runs the models and returns both strategies",
    )
    args = parser.parse_args(argv)

    engine = _load_engine(args.engine) if args.engine else None
    result = evaluate_models(
        Path(args.dataset),
        Path(args.out),
        engine=engine,
        weights_dir=args.weights,
        ffmpeg=args.ffmpeg,
        device=args.device,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "measured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
