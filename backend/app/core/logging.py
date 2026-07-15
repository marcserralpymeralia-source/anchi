from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.observability import current_context, redact_sensitive_data


_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__.keys())
_CONFIGURED = False


class StructuredFormatter(logging.Formatter):
    def __init__(self, *, output_format: str = "json"):
        super().__init__()
        self.output_format = output_format

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = current_context()
        if context:
            payload["context"] = context
        extra = {key: value for key, value in record.__dict__.items() if key not in _STANDARD_RECORD_KEYS and not key.startswith("_")}
        if extra:
            payload["extra"] = redact_sensitive_data(extra)
        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": str(record.exc_info[1]) if record.exc_info[1] else "",
            }
        payload = redact_sensitive_data(payload)
        if self.output_format == "text":
            context_text = ""
            if payload.get("context"):
                context_text = " " + json.dumps(payload["context"], ensure_ascii=False, sort_keys=True)
            return f"{payload['timestamp']} {payload['level'].upper()} {payload['logger']} - {payload['message']}{context_text}"
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    root = logging.getLogger()
    output_format = "json" if str(getattr(settings, "log_format", "json")).strip().lower() != "text" else "text"
    formatter = StructuredFormatter(output_format=output_format)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    root.setLevel(getattr(logging, str(getattr(settings, "log_level", "info")).upper(), logging.INFO))
    _CONFIGURED = True
