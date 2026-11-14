"""Model adapter boundary and the recognition contract gate.

The cloud worker must never silently choose a diarization/identity strategy that
has not been measured.  This module contains the narrow seam between the cloud
pipeline and whatever recogniser is eventually approved:

* :class:`ModelAdapter` is the protocol the pipeline calls.
* :class:`RecognitionParams` are the parameters copied from the evidence
  contract; they are only selectable when the contract documents an approved
  strategy with measured evidence.
* :func:`load_params` reads the contract, :func:`require_selected` refuses to
  run without an approved strategy, and :func:`build_adapter` refuses to build
  an adapter unless the contract carries measured evidence.

Nothing here imports the local spike, MLX, profiles or any global model cache.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from cloud.contracts import (
    AudioInput,
    CloudError,
    Embedding,
    TranscriptView,
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

STRATEGIES = ("mixed", "separate_merge")
LANGUAGES = ("zh-cn",)
CONTRACT_STATUS_APPROVED = "approved"
CONTRACT_STATUS_PROPOSED = "proposed_pending_measurement"


class EnhancementError(Exception):
    """Diarization or speaker-identity work failed after ASR text exists.

    The pipeline catches this (and a small set of audio/validation errors) and
    keeps the basic transcript: text is useful, identities are optional.
    """


class ModelNotConfigured(CloudError):
    """No measured, approved recognition strategy is available."""

    status_code = 503
    default_code = "model_not_configured"


class ModelAdapter(Protocol):
    """The only surface the cloud pipeline may call on a recogniser."""

    def transcribe(
        self, audio: AudioInput, speakers: int | None
    ) -> TranscriptView:  # pragma: no cover - protocol
        ...

    def embed(self, samples: Sequence[float]) -> Embedding:  # pragma: no cover
        ...


@dataclass(frozen=True)
class RecognitionParams:
    """Recognition parameters locked to the evidence contract.

    `strategy` is `None` until the contract records an approved strategy;
    every other field mirrors `proposed_parameters` in the contract.
    """

    strategy: str | None
    dedup_window_ms: int = 1200
    dedup_min_text_similarity: float = 0.9
    cluster_similarity_threshold: float = 0.75
    max_identities: int = 20
    language: str = "zh-cn"
    contract_status: str = CONTRACT_STATUS_PROPOSED


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"contract field {field} must be an object")
    return value


def _proposed_value(contract: Mapping[str, Any], field: str) -> Any:
    parameters = _require_object(
        contract.get("proposed_parameters"), "proposed_parameters"
    )
    entry = parameters.get(field)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ValueError(f"contract field proposed_parameters.{field}.value missing")
    return entry["value"]


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"contract field {field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"contract field {field} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_ratio(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"contract field {field} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"contract field {field} must be a finite ratio in [0, 1]")
    return number


def load_params(contract: Mapping[str, Any]) -> RecognitionParams:
    """Read parameters from the evidence contract without inventing values."""

    window = _bounded_int(
        _proposed_value(contract, "dedup_window_ms"), "dedup_window_ms", 0, 60_000
    )
    dedup_similarity = _bounded_ratio(
        _proposed_value(contract, "dedup_min_text_similarity"),
        "dedup_min_text_similarity",
    )
    cluster_threshold = _bounded_ratio(
        _proposed_value(contract, "cluster_similarity_threshold"),
        "cluster_similarity_threshold",
    )
    max_identities = _bounded_int(
        _proposed_value(contract, "max_identities"), "max_identities", 1, 10_000
    )

    language = contract.get("language", "zh-cn")
    if language not in LANGUAGES:
        raise ValueError(f"unsupported transcript language: {language!r}")

    recognition = _require_object(contract.get("recognition"), "recognition")
    strategy: str | None = None
    status = CONTRACT_STATUS_PROPOSED
    if recognition.get("strategy") == CONTRACT_STATUS_APPROVED:
        candidate = recognition.get("approved_strategy")
        if candidate in STRATEGIES:
            strategy = candidate
            status = CONTRACT_STATUS_APPROVED

    return RecognitionParams(
        strategy=strategy,
        dedup_window_ms=window,
        dedup_min_text_similarity=dedup_similarity,
        cluster_similarity_threshold=cluster_threshold,
        max_identities=max_identities,
        language=language,
        contract_status=status,
    )


def require_selected(params: RecognitionParams) -> str:
    """Return the approved strategy or refuse to start transcription."""

    if params.strategy is None or params.contract_status != CONTRACT_STATUS_APPROVED:
        raise ModelNotConfigured("model_not_configured")
    return params.strategy


class _InjectedAdapter:
    """Adapter assembled from explicitly injected engine implementations."""

    def __init__(self, asr: Any, speaker_embedding: Any) -> None:
        self._asr = asr
        self._speaker_embedding = speaker_embedding

    def transcribe(self, audio: AudioInput, speakers: int | None) -> TranscriptView:
        return self._asr.transcribe(audio, speakers)

    def embed(self, samples: Sequence[float]) -> Embedding:
        return self._speaker_embedding.embed(samples)


def build_adapter(
    contract: Mapping[str, Any], engines: Mapping[str, Any] | None = None
) -> ModelAdapter:
    """Build an adapter only when the contract records measured evidence.

    The check is deliberately small: the contract must name an approved
    strategy, and its evidence block must include metrics, a corpus reference,
    engine identities with checksums, and a calibration reference.  Engines
    are injected by the caller; this module never loads weights or models.
    """

    params = load_params(contract)
    strategy = require_selected(params)

    recognition = _require_object(contract.get("recognition"), "recognition")
    if recognition.get("approved_strategy") != strategy:
        raise ModelNotConfigured("model_not_configured")

    try:
        evidence = _require_object(recognition.get("evidence"), "recognition.evidence")
        metrics = _require_object(evidence.get("metrics"), "evidence.metrics")
        corpus = _require_object(evidence.get("corpus"), "evidence.corpus")
        calibration = _require_object(
            evidence.get("calibration"), "evidence.calibration"
        )
        raw_engines = _require_object(evidence.get("engines"), "evidence.engines")
        if not metrics or not corpus.get("id") or not calibration.get("version"):
            raise ModelNotConfigured("model_not_configured")
        if not raw_engines:
            raise ModelNotConfigured("model_not_configured")
        for role in ("asr", "speaker_embedding"):
            entry = raw_engines.get(role)
            if entry is None:
                continue
            entry = _require_object(entry, f"evidence.engines.{role}")
            for field in ("name", "version", "weights_sha256"):
                if entry.get(field) in (None, ""):
                    raise ModelNotConfigured("model_not_configured")
    except (ValueError, KeyError, TypeError):
        raise ModelNotConfigured("model_not_configured") from None

    supplied = engines or {}
    for role in ("asr", "speaker_embedding"):
        if supplied.get(role) is None:
            raise ModelNotConfigured("model_not_configured")

    return _InjectedAdapter(supplied["asr"], supplied["speaker_embedding"])
