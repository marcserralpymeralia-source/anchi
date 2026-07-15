from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any


_LOCK = Lock()
_STARTED_AT = datetime.now(timezone.utc)
_STARTED_MONOTONIC = monotonic()
_REQUEST_STATUS = Counter()
_REQUEST_ROUTES = Counter()
_JOB_STATUS = Counter()
_JOB_TYPES = Counter()
_LAST_REQUEST_AT: str | None = None
_LAST_JOB_AT: str | None = None
_REQUEST_COUNT = 0
_REQUEST_DURATION_TOTAL = 0.0
_JOB_COUNT = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_request(*, route: str | None, status_code: int, duration_seconds: float) -> None:
    global _LAST_REQUEST_AT, _REQUEST_COUNT, _REQUEST_DURATION_TOTAL
    with _LOCK:
        _REQUEST_COUNT += 1
        _REQUEST_DURATION_TOTAL += max(duration_seconds, 0.0)
        _REQUEST_STATUS[str(status_code)] += 1
        if route:
            _REQUEST_ROUTES[route] += 1
        _LAST_REQUEST_AT = _now()


def record_job(*, job_type: str | None, status: str) -> None:
    global _LAST_JOB_AT, _JOB_COUNT
    with _LOCK:
        _JOB_COUNT += 1
        _JOB_STATUS[status] += 1
        if job_type:
            _JOB_TYPES[job_type] += 1
        _LAST_JOB_AT = _now()


def snapshot_metrics() -> dict[str, Any]:
    with _LOCK:
        average_request_ms = 0.0
        if _REQUEST_COUNT:
            average_request_ms = (_REQUEST_DURATION_TOTAL / _REQUEST_COUNT) * 1000
        return {
            "started_at": _STARTED_AT.isoformat(),
            "uptime_seconds": int(monotonic() - _STARTED_MONOTONIC),
            "requests_total": _REQUEST_COUNT,
            "requests_by_status": dict(_REQUEST_STATUS),
            "requests_by_route": dict(_REQUEST_ROUTES),
            "requests_avg_duration_ms": round(average_request_ms, 2),
            "jobs_total": _JOB_COUNT,
            "jobs_by_status": dict(_JOB_STATUS),
            "jobs_by_type": dict(_JOB_TYPES),
            "last_request_at": _LAST_REQUEST_AT,
            "last_job_at": _LAST_JOB_AT,
        }
