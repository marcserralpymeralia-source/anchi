from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.contracts import (
    ConnectorCapability,
    ConnectorDeliveryError,
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

    def __init__(self, *, scenario: str = "success", identifier: str | None = None) -> None:
        if identifier:
            self.identifier = identifier
        self.created_orders: dict[str, str] = {}
        self.scenario = scenario
        self._health = ConnectorHealth(ConnectorStatus.HEALTHY, "Conector local determinista.")
        self.customer_fixtures = {"C001": {"external_id": "C001", "name": "Cliente Fake"}}
        self.product_fixtures = {"ART001": {"external_id": "ART001", "name": "Artículo Fake"}}

    @property
    def health(self) -> ConnectorHealth:
        return self._health

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario

    def set_health(self, status: ConnectorStatus, message: str | None = None) -> None:
        self._health = ConnectorHealth(status, message or status.value)

    def lookup_customer(self, external_id: str) -> dict[str, str] | None:
        return self.customer_fixtures.get(external_id)

    def lookup_product(self, external_id: str) -> dict[str, str] | None:
        return self.product_fixtures.get(external_id)

    def validate_order(self, db: Session, order) -> list[str]:  # noqa: ANN001
        errors: list[str] = []
        if self.scenario == "permanent_validation_error":
            errors.append("FakeERP rechazó la validación de forma permanente")
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

    def find_order_by_idempotency_key(self, db: Session, *, idempotency_key: str) -> str | None:
        return self.created_orders.get(idempotency_key)

    def create_order(self, db: Session, order, *, idempotency_key: str | None = None) -> DestinationOrderResult:  # noqa: ANN001
        if self._health.status in {ConnectorStatus.MISCONFIGURED, ConnectorStatus.NOT_CONFIGURED}:
            raise ConnectorDeliveryError(
                "FakeERP está mal configurado.",
                error_type="misconfigured",
                retryable=False,
            )
        if self._health.status in {ConnectorStatus.DEGRADED, ConnectorStatus.UNAVAILABLE}:
            raise ConnectorDeliveryError(
                "FakeERP no está disponible temporalmente.",
                error_type="connector_unavailable",
                retryable=True,
            )

        idempotency_key = idempotency_key or order.source_external_event_id or f"order:{order.id}"
        existing = self.created_orders.get(idempotency_key)
        if existing:
            return DestinationOrderResult(self.identifier, existing, status="already_exists", idempotent_replay=True)
        external_id = f"FAKE-ORDER-{len(self.created_orders) + 1:06d}"

        if self.scenario == "timeout_before_create":
            raise ConnectorDeliveryError("Timeout antes de crear el pedido remoto.", error_type="timeout", retryable=True)
        if self.scenario == "temporary_unavailable":
            raise ConnectorDeliveryError("FakeERP temporalmente no disponible.", error_type="temporary_unavailable", retryable=True)
        if self.scenario == "rate_limited":
            raise ConnectorDeliveryError("FakeERP ha limitado la tasa.", error_type="rate_limited", retryable=True)
        if self.scenario == "timeout_after_create":
            self.created_orders[idempotency_key] = external_id
            raise ConnectorDeliveryError(
                "Timeout después de crear el pedido remoto.",
                error_type="timeout_after_remote_commit",
                retryable=True,
                remote_commit_unknown=True,
            )

        self.created_orders[idempotency_key] = external_id
        return DestinationOrderResult(self.identifier, external_id)
