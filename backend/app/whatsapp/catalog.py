from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.identity import bind_external_identity
from app.connectors.whatsapp_catalog import (
    CATALOG_EXTERNAL_SYSTEM,
    WHATSAPP_CUSTOMER_EXTERNAL_SYSTEM,
    WhatsAppCatalogInboundConnector,
)
from app.db.models import InboundMessage
from app.orders.modular import process_inbound_order_event
from app.whatsapp.service import WhatsAppTenantConfig, whatsapp_catalog_event_matches_config, whatsapp_config


def bind_whatsapp_customer_identity(db: Session, *, company_id: int, customer_id: int, wa_id: str):
    return bind_external_identity(
        db,
        company_id=company_id,
        entity_type="customer",
        entity_id=customer_id,
        external_system=WHATSAPP_CUSTOMER_EXTERNAL_SYSTEM,
        external_id=wa_id,
        external_subtype="wa_id",
    )


def bind_catalog_product_identity(
    db: Session,
    *,
    company_id: int,
    product_id: int,
    product_retailer_id: str,
    catalog_id: str | None = None,
):
    return bind_external_identity(
        db,
        company_id=company_id,
        entity_type="product",
        entity_id=product_id,
        external_system=CATALOG_EXTERNAL_SYSTEM,
        external_id=product_retailer_id,
        external_subtype="product_retailer_id",
        metadata={"catalog_id": catalog_id} if catalog_id else None,
    )


def _stored_message_payload(message: InboundMessage) -> dict[str, Any]:
    try:
        payload = json.loads(message.raw_payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def is_whatsapp_catalog_order_message(message: InboundMessage) -> bool:
    payload = _stored_message_payload(message)
    raw_message = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return str(message.content_type or "").strip().lower() == "order" or str(raw_message.get("type") or "").strip().lower() == "order"


def _review_message(db: Session, message: InboundMessage, reason: str) -> dict[str, Any]:
    message.status = "doubtful"
    message.processing_step = "catalog_order_review"
    message.processing_error = reason[:2000]
    db.commit()
    return {
        "ok": False,
        "review_required": True,
        "message_id": message.id,
        "error_type": "catalog_order_review",
        "message": reason,
    }


def process_whatsapp_catalog_message(
    db: Session,
    message: InboundMessage,
    *,
    config: WhatsAppTenantConfig | None = None,
) -> dict[str, Any]:
    """Adapt one persisted Meta order message into the modular structured flow."""

    config = config or whatsapp_config(db, message.company_id)
    stored = _stored_message_payload(message)
    raw_message = stored.get("payload") if isinstance(stored.get("payload"), dict) else {}
    metadata = stored.get("metadata") if isinstance(stored.get("metadata"), dict) else {}
    connector_payload = {
        "message": raw_message,
        "sender": message.sender,
        "phone_number_id": metadata.get("phone_number_id"),
        "business_account_id": metadata.get("business_account_id"),
    }
    try:
        event = WhatsAppCatalogInboundConnector().to_inbound_order_event(
            connector_payload,
            company_id=message.company_id,
        )
    except ValueError as exc:
        return _review_message(db, message, str(exc))

    if not whatsapp_catalog_event_matches_config(
        {"message_type": "order", "catalog_id": event.source_catalog_id},
        config,
    ):
        return _review_message(db, message, "El catálogo WhatsApp no está autorizado para este tenant.")

    result = process_inbound_order_event(db, event, route=True)
    message.processing_step = "catalog_order_processed" if not result.order.review_reasons else "catalog_order_review"
    if result.order.review_reasons:
        message.status = "doubtful"
    message.order_id = result.order.id
    db.commit()
    return {
        "ok": True,
        "structured": True,
        "message_id": message.id,
        "order_id": result.order.id,
        "delivery_id": result.delivery.id if result.delivery else None,
        "review_required": bool(result.order.review_reasons),
    }
