from __future__ import annotations

import json
from typing import Any


def extraction_diagnostics_from_payload(raw_payload: str | None) -> dict[str, Any]:
    if not raw_payload:
        return {
            "source": "missing",
            "label": "Sin traza de extracción",
            "badge_class": "status-pending",
            "schema_version": None,
            "model": None,
            "requires_review": None,
            "uncertainty_count": 0,
            "fallback_reason": None,
        }
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return {
            "source": "invalid",
            "label": "Traza no legible",
            "badge_class": "warn",
            "schema_version": None,
            "model": None,
            "requires_review": None,
            "uncertainty_count": 0,
            "fallback_reason": None,
        }
    if not isinstance(payload, dict):
        return extraction_diagnostics_from_payload(None)
    meta = payload.get("_extraction_meta") if isinstance(payload.get("_extraction_meta"), dict) else {}
    source = meta.get("source") or "legacy_extraction"
    structured_payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    if source == "structured_order_extraction":
        label = "Extractor estructurado"
        badge_class = "status-confirmed"
    elif source == "legacy_extraction":
        label = "Extractor anterior"
        badge_class = "status-pending"
    else:
        label = str(source).replace("_", " ").title()
        badge_class = "status-pending"
    return {
        "source": source,
        "label": label,
        "badge_class": badge_class,
        "schema_version": meta.get("schemaVersion"),
        "model": meta.get("model"),
        "requires_review": structured_payload.get("requiresReview") if structured_payload else payload.get("requiere_revision_humana"),
        "uncertainty_count": _uncertainty_count(structured_payload or payload),
        "fallback_reason": meta.get("structuredFallbackReason"),
    }


def extraction_diagnostics_from_messages(messages: list[Any] | tuple[Any, ...] | None) -> dict[str, Any]:
    raw_payload = None
    for message in sorted(messages or [], key=lambda item: getattr(item, "id", 0) or 0, reverse=True):
        candidate = getattr(message, "extraction_json", None)
        if candidate:
            raw_payload = candidate
            break
    return extraction_diagnostics_from_payload(raw_payload)


def _uncertainty_count(payload: dict[str, Any]) -> int:
    count = len(payload.get("uncertainties") or [])
    lines = payload.get("lines") or (payload.get("pedido") or {}).get("lineas") or []
    if isinstance(lines, list):
        for line in lines:
            if isinstance(line, dict):
                count += len(line.get("uncertainties") or [])
    return count
