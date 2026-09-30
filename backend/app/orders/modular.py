from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.contracts import ConnectorRegistry, InboundOrderEvent, InputMode
from app.connectors.identity import resolve_customer_by_external_identity, resolve_product_by_external_identity
from app.connectors.routing import RoutingResult
from app.db.models import InboundMessage, Order, OrderDelivery, OrderLine
from app.messages.service import persist_normalized_message
from app.orders.delivery import enqueue_order_delivery


@dataclass(frozen=True, slots=True)
class ModularOrderResult:
    order: Order
    inbound_message: InboundMessage
    routing: RoutingResult | None = None
    delivery: OrderDelivery | None = None
    duplicate: bool = False


def _existing_order(db: Session, event: InboundOrderEvent) -> Order | None:
    return db.scalar(
        select(Order).where(
            Order.company_id == event.company_id,
            Order.source_provider == event.provider,
            Order.source_external_event_id == event.external_event_id,
        )
    )


def _review_reasons(order: Order) -> list[str]:
    reasons: list[str] = []
    if not order.validated_customer_id:
        reasons.append("Cliente externo no resuelto")
    for line in order.lines or []:
        if not line.validated_product_id:
            reasons.append(f"Producto externo no resuelto: {line.source_product_external_id}")
        if line.quantity is None or float(line.quantity) <= 0:
            reasons.append(f"Cantidad inválida en línea {line.source_product_external_id}")
    return list(dict.fromkeys(reasons))


def process_inbound_order_event(
    db: Session,
    event: InboundOrderEvent,
    *,
    registry: ConnectorRegistry | None = None,
    route: bool = True,
) -> ModularOrderResult:
    if event.source_mode is not InputMode.STRUCTURED:
        raise ValueError("Este flujo solo procesa eventos structured.")
    if event.company_id <= 0:
        raise ValueError("El evento necesita un tenant válido.")

    existing = _existing_order(db, event)
    if existing:
        inbound = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == event.company_id,
                InboundMessage.provider == event.provider,
                InboundMessage.source_external_id == event.external_event_id,
            )
        )
        if inbound is None:
            raise RuntimeError("El pedido duplicado no tiene su mensaje de entrada asociado.")
        return ModularOrderResult(order=existing, inbound_message=inbound, duplicate=True)

    inbound, conversation = persist_normalized_message(
        db,
        message=_normalized_message(event),
        content_type="structured_order",
    )
    existing = _existing_order(db, event)
    if existing:
        inbound.order_id = existing.id
        return ModularOrderResult(order=existing, inbound_message=inbound, duplicate=True)

    customer = None
    if event.customer_external_id:
        customer = resolve_customer_by_external_identity(
            db,
            company_id=event.company_id,
            external_system=event.external_system,
            external_id=event.customer_external_id,
        )
    order = Order(
        company_id=event.company_id,
        conversation_id=conversation.id,
        source_channel_key=event.connector_key,
        source_mode=event.source_mode.value,
        source_provider=event.provider,
        source_external_event_id=event.external_event_id,
        source_external_order_id=event.external_order_id,
        customer_id=customer.id if customer else None,
        validated_customer_id=customer.id if customer else None,
        customer_detected_name=event.customer_name or (customer.fiscal_name if customer else None),
        customer_identification_method="external_identity" if customer else "unresolved",
        customer_score=100 if customer else 0,
        notes=event.notes or "",
        status="pending_review",
        validation_status="pending",
        review_status="not_required",
        export_status="not_requested",
    )
    db.add(order)
    db.flush()
    for item in event.items:
        product = resolve_product_by_external_identity(
            db,
            company_id=event.company_id,
            external_system=event.external_system,
            external_id=item.source_product_id,
        )
        quantity_valid = item.quantity > 0
        db.add(
            OrderLine(
                company_id=event.company_id,
                order_id=order.id,
                product_id=product.id if product else None,
                validated_product_id=product.id if product and quantity_valid else None,
                source_product_system=event.external_system,
                source_product_external_id=item.source_product_id,
                original_text=item.description or item.source_product_id,
                detected_reference=product.reference if product else item.source_product_id,
                detected_product=product.name if product else item.description,
                quantity=item.quantity,
                unit=item.unit or "",
                unit_price_received=item.unit_price,
                currency_received=item.currency,
                extraction_confidence=0,
                line_score=100 if product and quantity_valid else 0,
                validation_status="validated" if product and quantity_valid else "pending",
                doubt_reason=None if product and quantity_valid else "Producto externo no resuelto o cantidad inválida",
            )
        )
    db.flush()
    order.review_reasons = "; ".join(_review_reasons(order)) or None
    if not order.review_reasons:
        order.validation_status = "validated"
        order.review_status = "not_required"
        order.status = "pedido_validado"
        order.export_status = "ready_to_export"
    else:
        order.validation_status = "needs_review"
        order.review_status = "required"
        order.status = "pending_review"
        order.export_status = "not_ready"
    inbound.order_id = order.id
    inbound.customer_id = customer.id if customer else None
    inbound.status = "processed" if not order.review_reasons else "doubtful"
    inbound.processing_step = "structured_order"
    # The connector registry is retained as a compatibility argument from
    # BLOQUE 1; external delivery now happens only through the outbox worker.
    delivery = enqueue_order_delivery(db, order) if route and not order.review_reasons else None
    routing = None
    db.flush()
    return ModularOrderResult(order=order, inbound_message=inbound, routing=routing, delivery=delivery)


def _normalized_message(event: InboundOrderEvent):
    from app.messages.service import NormalizedMessage

    return NormalizedMessage(
        company_id=event.company_id,
        channel_key=event.connector_key,
        provider=event.provider,
        external_id=event.external_event_id,
        sender=event.customer_external_id,
        subject="Structured order",
        text_content=None,
        external_thread_id=event.external_order_id or event.external_event_id,
        metadata=dict(event.raw_payload),
    )
