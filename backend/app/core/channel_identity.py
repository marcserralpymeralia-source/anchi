"""Canonical channel identity helpers used by inbox and review views."""

import json


EMAIL_PROVIDERS = {"email", "imap", "smtp", "gmail", "outlook", "microsoft", "exchange"}
WHATSAPP_PROVIDERS = {"whatsapp", "meta"}


def is_whatsapp_provider(provider: str | None) -> bool:
    return (provider or "").strip().lower() in WHATSAPP_PROVIDERS


def channel_label(channel_key: str | None) -> str:
    return {"email": "Email", "whatsapp": "WhatsApp", "inbound": "Entrada"}.get(
        (channel_key or "inbound").strip().lower(), "Entrada"
    )


def inbound_channel_key(message) -> str:  # noqa: ANN001
    """Return the channel declared by an inbound message."""
    provider = (getattr(message, "provider", "") or "").strip().lower()
    if is_whatsapp_provider(provider):
        return "whatsapp"
    raw_payload = getattr(message, "raw_payload_json", None)
    if raw_payload:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("import_type") == "manual_whatsapp":
            return "whatsapp"
    if provider in EMAIL_PROVIDERS or not provider:
        return "email"
    return "inbound"


def order_channel_key(order) -> str:  # noqa: ANN001
    """Resolve an order channel, keeping the linked email authoritative."""
    if getattr(order, "email_id", None):
        return "email"
    conversation = getattr(order, "conversation", None)
    provider = (getattr(conversation, "provider", "") or "").strip().lower() if conversation else ""
    if is_whatsapp_provider(provider):
        return "whatsapp"
    if provider in EMAIL_PROVIDERS:
        return "email"
    for message in (getattr(conversation, "messages", None) or []) if conversation else []:
        if inbound_channel_key(message) == "whatsapp":
            return "whatsapp"
    return "inbound"
