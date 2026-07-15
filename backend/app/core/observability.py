from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any


TRACE_PAYLOAD_KEY = "__trace"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
tenant_id_var: ContextVar[int | None] = ContextVar("tenant_id", default=None)
tenant_slug_var: ContextVar[str | None] = ContextVar("tenant_slug", default=None)
user_id_var: ContextVar[int | None] = ContextVar("user_id", default=None)
membership_id_var: ContextVar[int | None] = ContextVar("membership_id", default=None)
job_id_var: ContextVar[int | None] = ContextVar("job_id", default=None)
worker_id_var: ContextVar[str | None] = ContextVar("worker_id", default=None)
route_var: ContextVar[str | None] = ContextVar("route", default=None)
method_var: ContextVar[str | None] = ContextVar("method", default=None)

_CONTEXT_VARS: dict[str, ContextVar[Any | None]] = {
    "request_id": request_id_var,
    "correlation_id": correlation_id_var,
    "tenant_id": tenant_id_var,
    "tenant_slug": tenant_slug_var,
    "user_id": user_id_var,
    "membership_id": membership_id_var,
    "job_id": job_id_var,
    "worker_id": worker_id_var,
    "route": route_var,
    "method": method_var,
}

_SECRET_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "refresh_token",
    "access_token",
    "client_secret",
    "smtp_password",
    "imap_password",
    "private_key",
    "database_url",
    "dsn",
)


def _normalize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, Exception):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        key_name = str(key).lower()
        if any(marker in key_name for marker in _SECRET_MARKERS):
            redacted[str(key)] = "[redacted]"
        else:
            redacted[str(key)] = redact_sensitive_data(value)
    return redacted


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_data(item) for item in value]
    return _normalize(value)


@contextmanager
def observability_scope(**values: Any):
    tokens: list[tuple[ContextVar[Any | None], Any]] = []
    for name, context_var in _CONTEXT_VARS.items():
        value = values.get(name, None)
        if value is None:
            continue
        tokens.append((context_var, context_var.set(value)))
    try:
        yield
    finally:
        for context_var, token in reversed(tokens):
            context_var.reset(token)


def current_context() -> dict[str, Any]:
    return {name: value for name, context_var in _CONTEXT_VARS.items() if (value := context_var.get()) is not None}


def encode_trace_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(payload or {})
    trace = redact_sensitive_data(current_context())
    if trace:
        normalized[TRACE_PAYLOAD_KEY] = trace
    return normalized


def split_trace_payload(payload: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not payload:
        return {}, {}
    normalized = dict(payload)
    trace = normalized.pop(TRACE_PAYLOAD_KEY, {}) or {}
    if not isinstance(trace, dict):
        trace = {}
    return normalized, redact_sensitive_data(trace)


def encode_structured_message(message: str, *, metadata: dict[str, Any] | None = None) -> str:
    context = current_context()
    if not context and not metadata:
        return message
    payload: dict[str, Any] = {"message": message}
    if context:
        payload["context"] = context
    if metadata is not None:
        payload["metadata"] = redact_sensitive_data(metadata)
    return json.dumps(redact_sensitive_data(payload), ensure_ascii=False, sort_keys=True, default=str)


def decode_structured_message(raw_message: str | None) -> dict[str, Any]:
    if not raw_message:
        return {"message": "", "context": {}, "metadata": {}}
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return {"message": raw_message, "context": {}, "metadata": {}}
    if not isinstance(payload, dict) or "message" not in payload:
        return {"message": raw_message, "context": {}, "metadata": {}}
    context = payload.get("context") or {}
    metadata = payload.get("metadata") or {}
    if not isinstance(context, dict):
        context = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "message": str(payload.get("message") or ""),
        "context": redact_sensitive_data(context),
        "metadata": redact_sensitive_data(metadata),
    }

