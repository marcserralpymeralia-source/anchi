from __future__ import annotations

import json
import re
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.config import get_settings


_QUERY_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
_QUERY_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE_RE = re.compile(r"\s+")
_PERFORMANCE_LISTENER_ATTACHED = False

current_performance_var: ContextVar["PerformanceCollector | None"] = ContextVar("current_performance", default=None)

DISPLAY_KEYS = (
    "items",
    "orders",
    "jobs",
    "logs",
    "customers",
    "products",
    "alerts",
    "companies",
    "channels",
    "users",
    "emails",
    "history_items",
    "recent_processed_emails",
    "all_items",
    "rows",
)

IGNORED_CONTEXT_KEYS = {
    "request",
    "user",
    "filters",
    "pagination",
    "summary",
    "totals",
    "scoring",
    "alerts",
    "exports_by_order",
    "request_id",
    "correlation_id",
}


def performance_profiling_enabled() -> bool:
    try:
        settings = get_settings()
    except Exception:  # pragma: no cover - settings may be unavailable in isolated tests
        return False
    return bool(getattr(settings, "performance_profiling_enabled", False))


def normalize_sql_statement(statement: str | None) -> str:
    if not statement:
        return ""
    normalized = _QUERY_LITERAL_RE.sub("?", statement)
    normalized = _QUERY_NUMBER_RE.sub("?", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip().lower()
    return normalized


def _operation_from_sql(statement: str) -> str:
    normalized = normalize_sql_statement(statement)
    if not normalized:
        return "unknown"
    return normalized.split(" ", 1)[0]


def _count_displayed_items(context: dict[str, Any]) -> int:
    for key in DISPLAY_KEYS:
        value = context.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return len(value["items"])
    return 0


def _count_loaded_records(context: dict[str, Any]) -> int:
    total = 0
    for key, value in context.items():
        if key in IGNORED_CONTEXT_KEYS or key.startswith("_"):
            continue
        if isinstance(value, list):
            if value and all(not isinstance(item, (str, int, float, bool, bytes)) for item in value):
                total += len(value)
            elif key in DISPLAY_KEYS:
                total += len(value)
        elif isinstance(value, dict) and isinstance(value.get("items"), list):
            total += len(value["items"])
    return total


@dataclass
class PerformanceCollector:
    request_id: str | None = None
    correlation_id: str | None = None
    endpoint: str | None = None
    method: str | None = None
    scenario: str | None = None
    sql_duration_ms: float = 0.0
    sql_query_count: int = 0
    sql_duplicate_count: int = 0
    sql_max_duration_ms: float = 0.0
    template_duration_ms: float = 0.0
    response_size_bytes: int = 0
    loaded_record_count: int = 0
    displayed_item_count: int = 0
    status_code: int | None = None
    _query_counts: Counter[str] = field(default_factory=Counter, repr=False)
    _query_durations: Counter[str] = field(default_factory=Counter, repr=False)
    _query_operations: Counter[str] = field(default_factory=Counter, repr=False)

    def record_sql(self, statement: str | None, duration_seconds: float) -> None:
        normalized = normalize_sql_statement(statement)
        if not normalized:
            return
        duration_ms = max(duration_seconds, 0.0) * 1000
        self.sql_duration_ms += duration_ms
        self.sql_query_count += 1
        if duration_ms > self.sql_max_duration_ms:
            self.sql_max_duration_ms = duration_ms
        if self._query_counts[normalized] > 0:
            self.sql_duplicate_count += 1
        self._query_counts[normalized] += 1
        self._query_durations[normalized] += duration_ms
        self._query_operations[_operation_from_sql(statement)] += 1

    def record_template(self, context: dict[str, Any] | None, duration_seconds: float) -> None:
        self.template_duration_ms += max(duration_seconds, 0.0) * 1000
        if context:
            self.loaded_record_count += _count_loaded_records(context)
            self.displayed_item_count += _count_displayed_items(context)

    def record_response_size(self, size_bytes: int) -> None:
        if size_bytes > 0:
            self.response_size_bytes = size_bytes

    def as_headers(self) -> dict[str, str]:
        payload = self.to_dict()
        return {
            "X-Perf-Total-Ms": f"{payload['duration_ms']:.2f}",
            "X-Perf-SQL-Count": str(payload["sql_query_count"]),
            "X-Perf-SQL-Duration-Ms": f"{payload['sql_duration_ms']:.2f}",
            "X-Perf-SQL-Max-Ms": f"{payload['sql_max_duration_ms']:.2f}",
            "X-Perf-SQL-Duplicate-Count": str(payload["sql_duplicate_count"]),
            "X-Perf-Template-Ms": f"{payload['template_duration_ms']:.2f}",
            "X-Perf-Response-Size-Bytes": str(payload["response_size_bytes"]),
            "X-Perf-Loaded-Records": str(payload["loaded_record_count"]),
            "X-Perf-Displayed-Items": str(payload["displayed_item_count"]),
            "X-Perf-SQL-Top": json.dumps(payload["sql_top_queries"], ensure_ascii=False, default=str),
        }

    def to_dict(self, total_duration_ms: float | None = None, python_duration_ms: float | None = None) -> dict[str, Any]:
        top_queries = [
            {
                "statement": statement,
                "count": count,
                "duration_ms": round(self._query_durations[statement], 2),
                "operation": _operation_from_sql(statement),
            }
            for statement, count in self._query_counts.most_common(10)
            if count > 1
        ]
        duration_ms = round(total_duration_ms if total_duration_ms is not None else self.sql_duration_ms + self.template_duration_ms, 2)
        python_duration_ms = round(python_duration_ms if python_duration_ms is not None else max(duration_ms - self.sql_duration_ms - self.template_duration_ms, 0.0), 2)
        return {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "scenario": self.scenario,
            "duration_ms": duration_ms,
            "python_duration_ms": python_duration_ms,
            "sql_duration_ms": round(self.sql_duration_ms, 2),
            "sql_query_count": self.sql_query_count,
            "sql_duplicate_count": self.sql_duplicate_count,
            "sql_max_duration_ms": round(self.sql_max_duration_ms, 2),
            "template_duration_ms": round(self.template_duration_ms, 2),
            "response_size_bytes": self.response_size_bytes,
            "loaded_record_count": self.loaded_record_count,
            "displayed_item_count": self.displayed_item_count,
            "status_code": self.status_code,
            "sql_top_queries": top_queries,
        }


def current_performance() -> PerformanceCollector | None:
    return current_performance_var.get()


@contextmanager
def performance_scope(collector: PerformanceCollector | None):
    if collector is None:
        yield None
        return
    token = current_performance_var.set(collector)
    try:
        yield collector
    finally:
        current_performance_var.reset(token)


def start_performance(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    endpoint: str | None = None,
    method: str | None = None,
    scenario: str | None = None,
) -> PerformanceCollector | None:
    if not performance_profiling_enabled():
        return None
    return PerformanceCollector(
        request_id=request_id,
        correlation_id=correlation_id,
        endpoint=endpoint,
        method=method,
        scenario=scenario,
    )


def record_template_render(template_name: str, context: dict[str, Any] | None, duration_seconds: float) -> None:
    collector = current_performance()
    if not collector:
        return
    collector.record_template(context, duration_seconds)


def configure_performance() -> None:
    global _PERFORMANCE_LISTENER_ATTACHED
    if _PERFORMANCE_LISTENER_ATTACHED:
        return

    @event.listens_for(Engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        collector = current_performance()
        if not collector:
            return
        stack = conn.info.setdefault("_anchi_perf_query_stack", [])
        stack.append(time.perf_counter())

    @event.listens_for(Engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        collector = current_performance()
        stack = conn.info.get("_anchi_perf_query_stack")
        if not collector or not stack:
            return
        started_at = stack.pop()
        collector.record_sql(statement, time.perf_counter() - started_at)

    @event.listens_for(Engine, "handle_error")
    def _handle_error(exception_context):  # noqa: ANN001
        collector = current_performance()
        connection = getattr(exception_context, "connection", None)
        if not collector or connection is None:
            return
        stack = connection.info.get("_anchi_perf_query_stack")
        if stack:
            stack.pop()

    _PERFORMANCE_LISTENER_ATTACHED = True


configure_performance()

