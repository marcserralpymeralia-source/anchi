from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.contracts import (
    ConnectorCapability,
    ConnectorHealth,
    ConnectorStatus,
    DestinationConnector,
    DestinationOrderResult,
    InboundConnector,
    InboundOrderEvent,
    InputMode,
    StructuredOrderItem,
)


class FakeInboundConnector(InboundConnector):
    identifier = "fake_inbound"
    capabilities = frozenset()

    def to_inbound_order_event(self, payload: Mapping[str, Any], *, company_id: int) -> InboundOrderEvent:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("El pedido estructurado necesita event_id.")

        raw_items = payload.get("items") or []
        items: list[StructuredOrderItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("Cada item estructurado debe ser un objeto.")
            source_product_id = str(raw_item.get("sku") or raw_item.get("product_external_id") or "").strip()
            if not source_product_id:
                raise ValueError("Cada item estructurado necesita sku.")
            try:
                quantity = float(raw_item.get("quantity"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Cantidad invalida para {source_product_id}.") from exc
            if quantity <= 0:
                raise ValueError(f"La cantidad debe ser positiva para {source_product_id}.")
            items.append(
                StructuredOrderItem(
                    source_product_id=source_product_id,
                    quantity=quantity,
                    unit=str(raw_item.get("unit") or "").strip() or None,
                    unit_price=raw_item.get("unit_price"),
                    currency=str(raw_item.get("currency") or "").strip() or None,
                    description=str(raw_item.get("description") or "").strip() or None,
                )
            )
        if not items:
            raise ValueError("El pedido estructurado necesita al menos una linea.")

        return InboundOrderEvent(
            company_id=company_id,
            connector_key=self.identifier,
            provider="fake_inbound",
            source_mode=InputMode.STRUCTURED,
            external_event_id=event_id,
            external_system=str(payload.get("external_system") or "fake_erp").strip(),
            external_order_id=str(payload.get("order_id") or "").strip() or None,
            customer_external_id=str(payload.get("customer_external_id") or "").strip() or None,
            customer_name=str(payload.get("customer_name") or "").strip() or None,
            items=tuple(items),
            notes=str(payload.get("notes") or "").strip() or None,
            raw_payload=dict(payload),
        )


class FakeERPConnector(DestinationConnector):
    identifier = "fake_erp"
    capabilities = frozenset(
        {
            ConnectorCapability.CUSTOMERS_READ,
            ConnectorCapability.PRODUCTS_READ,
            ConnectorCapability.ORDERS_CREATE,
            ConnectorCapability.ORDERS_READ,
        }
    )

    def __init__(self) -> None:
        self.created_orders: dict[str, str] = {}
        self.customer_fixtures = {"C001": {"external_id": "C001", "name": "Cliente Fake"}}
        self.product_fixtures = {"ART001": {"external_id": "ART001", "name": "Artículo Fake"}}

    @property
    def health(self) -> ConnectorHealth:
        return ConnectorHealth(ConnectorStatus.READY, "Conector local determinista.")

    def lookup_customer(self, external_id: str) -> dict[str, str] | None:
        return self.customer_fixtures.get(external_id)

    def lookup_product(self, external_id: str) -> dict[str, str] | None:
        return self.product_fixtures.get(external_id)

    def validate_order(self, db: Session, order) -> list[str]:  # noqa: ANN001
        errors: list[str] = []
        if not order.validated_customer_id:
            errors.append("Cliente no validado")
        if not order.lines:
            errors.append("El pedido no tiene líneas")
        for line in order.lines or []:
            if not line.validated_product_id:
                errors.append(f"Producto no validado: {line.source_product_external_id or line.detected_reference or line.id}")
            if line.quantity is None or float(line.quantity) <= 0:
                errors.append(f"Cantidad inválida en línea {line.id}")
        return errors

    def create_order(self, db: Session, order) -> DestinationOrderResult:  # noqa: ANN001
        idempotency_key = order.source_external_event_id or f"order:{order.id}"
        existing = self.created_orders.get(idempotency_key)
        if existing:
            return DestinationOrderResult(self.identifier, existing, status="already_exists", idempotent_replay=True)
        external_id = f"FAKE-ORDER-{len(self.created_orders) + 1:06d}"
        self.created_orders[idempotency_key] = external_id
        return DestinationOrderResult(self.identifier, external_id)
