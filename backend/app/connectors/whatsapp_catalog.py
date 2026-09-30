from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.connectors.contracts import InboundConnector, InboundOrderEvent, InputMode, StructuredOrderItem


CATALOG_EXTERNAL_SYSTEM = "meta_whatsapp_catalog"
WHATSAPP_CUSTOMER_EXTERNAL_SYSTEM = "meta_whatsapp"
WHATSAPP_CHANNEL_KEY = "whatsapp"


def _decimal_number(value: Any, *, field: str, positive: bool = False) -> float:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} no es numérico.") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"{field} no es válido.")
    result = float(number)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{field} no es válido.")
    return result


class WhatsAppCatalogInboundConnector(InboundConnector):
    """Adapt Meta order messages to the neutral structured inbound contract."""

    identifier = "whatsapp_catalog"

    def to_inbound_order_event(self, payload: Mapping[str, Any], *, company_id: int) -> InboundOrderEvent:
        message = payload.get("message") if isinstance(payload.get("message"), Mapping) else payload
        if str(message.get("type") or "").strip().lower() != "order":
            raise ValueError("El mensaje WhatsApp no es un pedido de catálogo.")

        external_event_id = str(message.get("id") or "").strip()
        if not external_event_id:
            raise ValueError("El pedido WhatsApp necesita wamid.")
        order_payload = message.get("order") if isinstance(message.get("order"), Mapping) else {}
        catalog_id = str(order_payload.get("catalog_id") or "").strip() or None
        raw_items = order_payload.get("product_items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("El pedido WhatsApp necesita product_items.")

        items: list[StructuredOrderItem] = []
        currencies: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("Cada product_item de WhatsApp debe ser un objeto.")
            retailer_id = str(raw_item.get("product_retailer_id") or "").strip()
            if not retailer_id:
                raise ValueError("Cada product_item necesita product_retailer_id.")
            quantity = _decimal_number(raw_item.get("quantity"), field="quantity", positive=True)
            unit_price = raw_item.get("item_price")
            parsed_price = None if unit_price in {None, ""} else _decimal_number(unit_price, field="item_price")
            currency = str(raw_item.get("currency") or "").strip().upper() or None
            if currency is not None and (len(currency) != 3 or not currency.isalpha()):
                raise ValueError("currency no es válida.")
            if currency is not None:
                currencies.add(currency)
            items.append(
                StructuredOrderItem(
                    source_product_id=retailer_id,
                    quantity=quantity,
                    unit="uds",
                    unit_price=parsed_price,
                    currency=currency,
                    source_product_system=CATALOG_EXTERNAL_SYSTEM,
                )
            )
        if len(currencies) > 1:
            raise ValueError("El pedido WhatsApp contiene monedas mezcladas.")

        sender = str(payload.get("sender") or message.get("from") or "").strip() or None
        return InboundOrderEvent(
            company_id=company_id,
            connector_key=WHATSAPP_CHANNEL_KEY,
            provider="meta",
            source_mode=InputMode.STRUCTURED,
            external_event_id=external_event_id,
            external_system=CATALOG_EXTERNAL_SYSTEM,
            customer_external_system=WHATSAPP_CUSTOMER_EXTERNAL_SYSTEM,
            external_order_id=external_event_id,
            customer_external_id=sender,
            items=tuple(items),
            notes=str(order_payload.get("text") or "").strip() or None,
            source_catalog_id=catalog_id,
            raw_payload=dict(payload),
        )
