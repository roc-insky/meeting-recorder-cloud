"""Bounded observability and fixed error rendering for the cloud API.

Nothing in this module accepts free-form request content. Metric names, label
keys and label values are allowlisted here; anything else is dropped before it
reaches a log handler. The only identifiers a log line may carry are a
server-generated request id and fixed stage/action/outcome names.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
from typing import Any, Mapping

from cloud.contracts import CloudError

_METRIC_LOGGER = logging.getLogger("cloud.metrics")
_EVENT_LOGGER = logging.getLogger("cloud.events")

_REQUEST_ID = re.compile(r"^req_[0-9a-f]{32}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: name -> allowed label keys. Values are separately allowlisted below.
METRIC_LABELS: dict[str, frozenset[str]] = {
    "upload_ack_latency_ms": frozenset({"outcome"}),
    "upload_chunk": frozenset({"outcome"}),
    "upload_chunk_missing": frozenset({"outcome"}),
    "task_queue": frozenset({"outcome"}),
    "task_queue_depth": frozenset({"bucket"}),
    "worker_failure": frozenset({"stage"}),
    "ai_provider": frozenset({"state"}),
    "access_rejection": frozenset({"reason"}),
    "request_latency_ms": frozenset({"route", "outcome"}),
    "rate_limit_exceeded": frozenset({"action"}),
}

_LABEL_VALUES: dict[str, frozenset[str]] = {
    "outcome": frozenset(
        {"ok", "error", "accepted", "duplicate", "claimed", "empty", "busy", "missing"}
    ),
    "bucket": frozenset({"0", "1-10", "11-100", "100+"}),
    "stage": frozenset({"claim", "slot", "decode", "publish", "processing"}),
    "state": frozenset({"live", "degraded"}),
    "reason": frozenset({"csrf", "forbidden", "not_found", "rate_limited"}),
    "route": frozenset(
        {
            "/api/cloud/uploads/{upload_id}/ack",
            "/api/cloud/uploads/{upload_id}/chunks/{epoch_id}/{track_id}/{sequence}",
            "/api/cloud/meetings/{meeting_id}/audio",
            "/api/cloud/meetings/{meeting_id}/summary",
            "/api/cloud/positions/{position_id}/interview",
        }
    ),
    "action": frozenset(
        {
            "chunk_write",
            "lease_write",
            "task_enqueue",
            "ai_call",
            "playback",
            "voiceprint_enroll",
        }
    ),
}

_MAX_METRIC_VALUE = 1_000_000_000.0


def new_request_id() -> str:
    """Server-generated, unguessable request id; a client value is never used."""
    return "req_" + secrets.token_hex(16)


def get_request_id(request: Any) -> str | None:
    """Read the middleware-assigned id, or None outside an HTTP request."""
    state = getattr(request, "state", None)
    value = getattr(state, "request_id", None)
    return value if isinstance(value, str) and _REQUEST_ID.match(value) else None


def error_payload(code: str, request_id: str | None) -> dict[str, str]:
    """The one fixed error shape: machine code plus server request id."""
    payload = {"error": code}
    if request_id:
        payload["request_id"] = request_id
    return payload


def safe_error(exc: Exception) -> tuple[int, str]:
    """Map an exception to a fixed status/code without reading its text."""
    if isinstance(exc, CloudError):
        return exc.status_code, exc.code
    return 500, "internal_error"


def emit_metric(
    name: str, value: float, labels: dict[str, str] | None = None
) -> None:
    """Emit one bounded metric line; invalid input is dropped, never logged raw."""
    allowed_labels = METRIC_LABELS.get(name)
    numeric = _finite_value(value)
    if allowed_labels is None or numeric is None:
        _drop_metric()
        return
    safe_labels: dict[str, str] = {}
    for key, label_value in (labels or {}).items():
        if key not in allowed_labels:
            _drop_metric()
            return
        if not isinstance(label_value, str) or label_value not in _LABEL_VALUES.get(
            key, frozenset()
        ):
            _drop_metric()
            return
        safe_labels[key] = label_value
    _METRIC_LOGGER.info(
        "%s",
        json.dumps(
            {"event": "metric", "name": name, "value": numeric, "labels": safe_labels},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def log_event(
    event: str,
    *,
    request_id: str | None = None,
    code: str | None = None,
    stage: str | None = None,
    route: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
) -> None:
    """Write a fixed structured application log line; unsafe fields are omitted."""
    if event not in _EVENTS:
        return
    payload: dict[str, Any] = {"event": event}
    if request_id and _REQUEST_ID.match(request_id):
        payload["request_id"] = request_id
    if code and _CODE.match(code):
        payload["code"] = code
    if stage in _STAGES:
        payload["stage"] = stage
    if route in _ROUTES:
        payload["route"] = route
    if action in _ACTIONS:
        payload["action"] = action
    if outcome in _OUTCOMES:
        payload["outcome"] = outcome
    _EVENT_LOGGER.info(
        "%s",
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )


def _finite_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or abs(numeric) > _MAX_METRIC_VALUE:
        return None
    return numeric


def _drop_metric() -> None:
    _METRIC_LOGGER.info(
        "%s",
        json.dumps(
            {"event": "metric_dropped"},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


_EVENTS = frozenset(
    {
        "http_rejected",
        "http_unhandled",
        "csrf_rejected",
        "rate_limited",
        "body_too_large",
        "worker_busy",
        "worker_failure",
    }
)
_STAGES = frozenset({"claim", "slot", "decode", "publish", "processing"})
_ROUTES = _LABEL_VALUES["route"]
_ACTIONS = _LABEL_VALUES["action"]
_OUTCOMES = _LABEL_VALUES["outcome"]
