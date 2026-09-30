from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.contracts import ConnectorRegistry, DestinationOrderResult
from app.db.models import Order, OrderRoute


@dataclass(frozen=True, slots=True)
class RoutingResult:
    order: Order
    destination: DestinationOrderResult | None = None
    errors: tuple[str, ...] = ()


def resolve_order_route(db: Session, *, company_id: int, order_type: str = "order") -> OrderRoute | None:
    return db.scalar(
        select(OrderRoute).where(
            OrderRoute.company_id == company_id,
            OrderRoute.order_type == order_type,
            OrderRoute.is_active.is_(True),
        )
    )


def route_order(db: Session, order: Order, registry: ConnectorRegistry) -> RoutingResult:
    if order.export_status == "exported" and order.destination_external_order_id:
        return RoutingResult(
            order=order,
            destination=DestinationOrderResult(
                connector_key=order.destination_connector_key or "",
                external_order_id=order.destination_external_order_id,
                status="already_exists",
                idempotent_replay=True,
            ),
        )
    route = resolve_order_route(db, company_id=order.company_id)
    if route is None:
        order.export_status = "not_configured"
        order.destination_connector_key = None
        return RoutingResult(order=order, errors=("No hay destino configurado para pedidos.",))

    order.destination_connector_key = route.destination_connector_key
    connector = registry.get(route.destination_connector_key)
    if connector is None:
        order.export_status = "failed"
        order.export_last_error = f"Connector no registrado: {route.destination_connector_key}"
        return RoutingResult(order=order, errors=(order.export_last_error,))

    if not hasattr(connector, "validate_order") or not hasattr(connector, "create_order"):
        order.export_status = "failed"
        order.export_last_error = f"El connector {route.destination_connector_key} no es de destino."
        return RoutingResult(order=order, errors=(order.export_last_error,))

    errors = tuple(connector.validate_order(db, order))
    if errors:
        order.export_status = "validation_failed"
        order.validation_status = "needs_review"
        order.review_status = "required"
        order.status = "pending_review"
        order.review_reasons = "; ".join(errors)
        return RoutingResult(order=order, errors=errors)

    order.export_attempt_count = int(order.export_attempt_count or 0) + 1
    order.export_status = "exporting"
    result = connector.create_order(db, order, idempotency_key=f"{order.company_id}/order/{order.id}/{route.destination_connector_key}")
    order.destination_external_order_id = result.external_order_id
    order.export_status = "exported"
    order.export_last_error = None
    order.status = "pedido_exportado"
    return RoutingResult(order=order, destination=result)
