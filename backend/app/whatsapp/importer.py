from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any


_PATTERNS = (
    re.compile(
        r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<sender>[^:]{1,80}):\s*(?P<text>.*)$"
    ),
    re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(?P<sender>[^:]{1,80}):\s*(?P<text>.*)$"
    ),
    re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*(?P<sep>[-|])\s*(?P<sender>[^:]{1,80}):\s*(?P<text>.*)$"
    ),
    re.compile(r"^(?P<sender>[^:]{1,80}):\s*(?P<text>.*)$"),
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _parse_datetime(date_text: str | None, time_text: str | None) -> str | None:
    if not date_text or not time_text:
        return None
    normalized_date = date_text.replace("-", "/").strip()
    normalized_time = time_text.strip()
    candidates = (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
    )
    for fmt in candidates:
        try:
            return datetime.strptime(f"{normalized_date} {normalized_time}", fmt).isoformat(sep=" ")
        except ValueError:
            continue
    return None


def _extract_attachment_hint(text: str) -> tuple[str, str | None]:
    lowered = text.lower()
    if "audio" in lowered or lowered.startswith("🎤"):
        return "Audio adjunto", "audio"
    if any(marker in lowered for marker in (".pdf", "pdf adjunto", "documento")) or re.search(r"\bpdf\b", lowered):
        return "Documento adjunto", "document"
    if any(marker in lowered for marker in (".png", ".jpg", ".jpeg", ".webp", "imagen")):
        return "Imagen adjunta", "image"
    return text, None


def parse_manual_whatsapp_text(
    raw_text: str,
    *,
    client_participant: str = "",
    company_participant: str = "",
) -> dict[str, Any]:
    text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = text.splitlines()
    messages: list[dict[str, Any]] = []
    warnings: list[str] = []

    client_norm = _normalize_text(client_participant)
    company_norm = _normalize_text(company_participant)
    seen_senders: list[str] = []
    current_message: dict[str, Any] | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        matched = None
        for pattern in _PATTERNS:
            matched = pattern.match(line)
            if matched:
                break
        if matched:
            sender = (matched.groupdict().get("sender") or "").strip()
            message_text = (matched.groupdict().get("text") or "").strip()
            if sender and sender not in seen_senders:
                seen_senders.append(sender)
            direction = "inbound"
            sender_norm = _normalize_text(sender)
            if company_norm and sender_norm == company_norm:
                direction = "outbound"
            elif client_norm and sender_norm == client_norm:
                direction = "inbound"
            elif not client_norm and len(seen_senders) == 1:
                direction = "inbound"
            elif company_participant and sender:
                direction = "outbound" if sender_norm == company_norm else "inbound"
            timestamp = _parse_datetime(matched.groupdict().get("date"), matched.groupdict().get("time"))
            message_text, attachment_kind = _extract_attachment_hint(message_text)
            current_message = {
                "timestamp": timestamp,
                "timestamp_label": timestamp[:16] if timestamp else "",
                "sender": sender or "Sin remitente",
                "direction": direction,
                "text": message_text,
                "attachments_referenced": [attachment_kind] if attachment_kind else [],
                "raw_line": line,
            }
            messages.append(current_message)
            continue
        if current_message:
            current_message["text"] = f"{current_message['text']}\n{line}".strip()
            current_message["raw_line"] = f"{current_message['raw_line']}\n{line}"
        else:
            warnings.append(f"No se pudo interpretar la línea: {line[:120]}")

    if not messages and text:
        warnings.append("No se detectó un formato de conversación compatible.")
        messages.append(
            {
                "timestamp": None,
                "timestamp_label": "",
                "sender": client_participant or "Cliente",
                "direction": "inbound",
                "text": text,
                "attachments_referenced": [],
                "raw_line": text[:240],
            }
        )

    if not client_participant and seen_senders:
        client_participant = seen_senders[0]
    if not company_participant and len(seen_senders) > 1:
        company_participant = seen_senders[1]

    normalized_text = "\n".join(f"{message['sender']}: {message['text']}".strip() for message in messages if message.get("text"))
    hash_source = "|".join(
        [
            _normalize_text(client_participant),
            _normalize_text(company_participant),
            normalized_text,
        ]
    ).encode("utf-8", errors="ignore")
    dedup_hash = hashlib.sha256(hash_source).hexdigest()
    thread_key = f"manual-whatsapp-{dedup_hash[:24]}"

    return {
        "raw_text": text,
        "messages": messages,
        "participants": {
            "client": client_participant.strip(),
            "company": company_participant.strip(),
            "all": seen_senders,
        },
        "warnings": warnings,
        "normalized_text": normalized_text,
        "dedupe_hash": dedup_hash,
        "thread_key": thread_key,
    }
