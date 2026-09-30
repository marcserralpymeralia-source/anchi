from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.contracts import (
    ConnectorDeliveryError,
    ConnectorRegistry,
    ConnectorStatus,
    DestinationOrderResult,
)
from app.connectors.fake import FakeERPConnector
from app.connectors.routing import resolve_order_route
from app.core.config import get_settings
from app.db.models import BackgroundJob, Order, OrderDelivery, OrderDeliveryAttempt
from app.jobs.service import enqueue_job, retry_job
from app.logs.service import log_flow_event


DELIVERY_JOB_TYPE = "deliver_order"
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class DeliveryExecutionResult:
    delivery: OrderDelivery
    destination: DestinationOrderResult | None = None
    reconciled: bool = False


class DeliveryProcessingError(ConnectorDeliveryError):
    """Failure exposed to the existing BackgroundJob retry machinery."""


_DEFAULT_DESTINATION_REGISTRY = ConnectorRegistry([FakeERPConnector()])


def default_destination_registry() -> ConnectorRegistry:
    return _DEFAULT_DESTINATION_REGISTRY


def _now() -> datetime:
    return datetime.now(timezone.utc)


def delivery_idempotency_key(order: Order, destination_connector_key: str) -> str:
    return f"{order.company_id}/order/{order.id}/{destination_connector_key}"


def delivery_job_dedupe_key(idempotency_key: str) -> str:
    return f"order-delivery:{idempotency_key}"


def retry_delay_seconds(attempt_number: int, *, base_seconds: int | None = None, max_seconds: int | None = None) -> int:
    settings = get_settings()
    base = max(0, int(base_seconds if base_seconds is not None else settings.job_retry_base_seconds))
    maximum = max(base, int(max_seconds if max_seconds is not None else settings.job_retry_max_seconds))
    return min(maximum, base * (2 ** max(0, attempt_number - 1)))


def enqueue_order_delivery(
    db: Session,
    order: Order,
    *,
    order_type: str = "order",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> OrderDelivery | None:
    """Create the outbox row and its generic worker job in one DB commit."""

    route = resolve_order_route(db, company_id=order.company_id, order_type=order_type)
    if route is None:
        order.export_status = "not_configured"
        order.export_last_error = "No hay destino configurado para pedidos."
        return None

    destination_key = route.destination_connector_key
    idempotency_key = delivery_idempotency_key(order, destination_key)
    existing = db.scalar(
        select(OrderDelivery).where(
            OrderDelivery.company_id == order.company_id,
            OrderDelivery.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing

    now = _now()
    order.destination_connector_key = destination_key
    order.export_status = "ready_to_export"
    order.export_last_error = None
    delivery = OrderDelivery(
        company_id=order.company_id,
        order_id=order.id,
        order_type=order_type,
        destination_connector_key=destination_key,
        idempotency_key=idempotency_key,
        status="queued",
        max_attempts=max(1, int(max_attempts)),
        next_attempt_at=now,
    )
    db.add(delivery)
    db.flush()
    enqueue_job(
        db,
        company_id=order.company_id,
        job_type=DELIVERY_JOB_TYPE,
        payload={"delivery_id": delivery.id},
        dedupe_key=delivery_job_dedupe_key(idempotency_key),
        max_retries=max(0, int(max_attempts) - 1),
    )
    db.refresh(delivery)
    log_flow_event(
        db,
        company_id=order.company_id,
        event="order.delivery.queued",
        stage="delivery",
        message="Entrega de pedido añadida al outbox.",
        entity_type="order_delivery",
        entity_id=delivery.id,
        status=delivery.status,
        metadata={
            "order_id": order.id,
            "connector": destination_key,
            "idempotency_key": idempotency_key,
        },
    )
    return delivery


def get_order_delivery(db: Session, *, company_id: int, delivery_id: int) -> OrderDelivery | None:
    delivery = db.get(OrderDelivery, delivery_id)
    if not delivery or delivery.company_id != company_id:
        return None
    return delivery


def list_failed_deliveries(db: Session, *, company_id: int, limit: int = 50) -> list[OrderDelivery]:
    return db.scalars(
        select(OrderDelivery)
        .where(OrderDelivery.company_id == company_id, OrderDelivery.status == "failed")
        .order_by(OrderDelivery.updated_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).all()


def _start_attempt(db: Session, delivery: OrderDelivery) -> OrderDeliveryAttempt:
    now = _now()
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    delivery.last_attempt_at = now
    delivery.next_attempt_at = None
    delivery.status = "delivering"
    attempt = OrderDeliveryAttempt(
        company_id=delivery.company_id,
        delivery_id=delivery.id,
        attempt_number=delivery.attempt_count,
        status="running",
        started_at=now,
    )
    db.add(attempt)
    db.flush()
    return attempt


def _finish_attempt(
    attempt: OrderDeliveryAttempt,
    *,
    status: str,
    external_order_id: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    next_attempt_at: datetime | None = None,
) -> None:
    attempt.status = status
    attempt.external_order_id = external_order_id
    attempt.error_type = error_type
    attempt.error_message = error_message[:2000] if error_message else None
    attempt.next_attempt_at = next_attempt_at
    attempt.finished_at = _now()


def _mark_success(
    db: Session,
    delivery: OrderDelivery,
    order: Order,
    attempt: OrderDeliveryAttempt,
    external_order_id: str,
    *,
    status: str = "succeeded",
) -> DeliveryExecutionResult:
    now = _now()
    _finish_attempt(attempt, status=status, external_order_id=external_order_id)
    delivery.status = "exported"
    delivery.destination_external_order_id = external_order_id
    delivery.last_error = None
    delivery.last_error_type = None
    delivery.next_attempt_at = None
    delivery.completed_at = now
    delivery.updated_at = now
    order.destination_connector_key = delivery.destination_connector_key
    order.destination_external_order_id = external_order_id
    order.export_status = "exported"
    order.export_last_error = None
    order.status = "pedido_exportado"
    order.exported_at = order.exported_at or now
    return DeliveryExecutionResult(
        delivery=delivery,
        destination=DestinationOrderResult(
            connector_key=delivery.destination_connector_key,
            external_order_id=external_order_id,
            status="already_exists" if status == "reconciled" else "created",
            idempotent_replay=status == "reconciled",
        ),
        reconciled=status == "reconciled",
    )


def _mark_failure(
    delivery: OrderDelivery,
    order: Order,
    attempt: OrderDeliveryAttempt,
    *,
    error_type: str,
    message: str,
    retryable: bool,
) -> bool:
    now = _now()
    can_retry = retryable and delivery.attempt_count < max(1, int(delivery.max_attempts or 1))
    next_attempt_at = now + timedelta(seconds=retry_delay_seconds(delivery.attempt_count)) if can_retry else None
    _finish_attempt(
        attempt,
        status="retry_scheduled" if can_retry else "failed_permanent",
        error_type=error_type,
        error_message=message,
        next_attempt_at=next_attempt_at,
    )
    delivery.last_error = message[:2000]
    delivery.last_error_type = error_type
    delivery.next_attempt_at = next_attempt_at
    delivery.status = "retrying" if can_retry else "failed"
    delivery.updated_at = now
    order.export_last_error = message[:2000]
    order.export_status = "retry_scheduled" if can_retry else "export_failed"
    if not can_retry:
        order.status = "error_exportacion"
    return can_retry


def _raise_delivery_failure(
    delivery: OrderDelivery,
    order: Order,
    attempt: OrderDeliveryAttempt,
    failure: ConnectorDeliveryError,
) -> None:
    can_retry = _mark_failure(
        delivery,
        order,
        attempt,
        error_type=failure.error_type,
        message=str(failure),
        retryable=failure.retryable,
    )
    raise DeliveryProcessingError(
        str(failure),
        error_type=failure.error_type,
        retryable=can_retry,
        remote_commit_unknown=failure.remote_commit_unknown,
    )


def process_order_delivery(
    db: Session,
    delivery: OrderDelivery,
    *,
    registry: ConnectorRegistry | None = None,
) -> DeliveryExecutionResult:
    """Deliver one outbox row; the caller owns the surrounding transaction."""

    order = db.scalar(select(Order).where(Order.id == delivery.order_id, Order.company_id == delivery.company_id))
    if order is None:
        attempt = _start_attempt(db, delivery)
        failure = ConnectorDeliveryError("No se encontró el pedido de la entrega.", error_type="order_missing")
        _finish_attempt(attempt, status="failed_permanent", error_type=failure.error_type, error_message=str(failure))
        delivery.status = "failed"
        delivery.last_error = str(failure)
        delivery.last_error_type = failure.error_type
        delivery.updated_at = _now()
        raise DeliveryProcessingError(str(failure), error_type=failure.error_type, retryable=False)

    if delivery.status == "exported" and delivery.destination_external_order_id:
        return DeliveryExecutionResult(
            delivery=delivery,
            destination=DestinationOrderResult(
                connector_key=delivery.destination_connector_key,
                external_order_id=delivery.destination_external_order_id,
                status="already_exists",
                idempotent_replay=True,
            ),
        )

    connector_registry = registry or default_destination_registry()
    connector = connector_registry.get(delivery.destination_connector_key)
    attempt = _start_attempt(db, delivery)
    order.export_attempt_count = int(order.export_attempt_count or 0) + 1
    order.export_status = "exporting"
    if connector is None:
        _raise_delivery_failure(
            delivery,
            order,
            attempt,
            ConnectorDeliveryError(
                f"Connector no registrado: {delivery.destination_connector_key}",
                error_type="connector_not_registered",
            ),
        )

    health = connector.health
    if health.status in {ConnectorStatus.MISCONFIGURED, ConnectorStatus.NOT_CONFIGURED}:
        _raise_delivery_failure(
            delivery,
            order,
            attempt,
            ConnectorDeliveryError(health.message or "Connector mal configurado.", error_type="misconfigured"),
        )
    if health.status in {ConnectorStatus.DEGRADED, ConnectorStatus.UNAVAILABLE}:
        _raise_delivery_failure(
            delivery,
            order,
            attempt,
            ConnectorDeliveryError(
                health.message or "Connector temporalmente no disponible.",
                error_type="connector_unavailable",
                retryable=True,
            ),
        )

    validation_errors = tuple(connector.validate_order(db, order))
    if validation_errors:
        _raise_delivery_failure(
            delivery,
            order,
            attempt,
            ConnectorDeliveryError(
                "; ".join(validation_errors),
                error_type="permanent_validation_error",
            ),
        )

    try:
        reconciled_id = connector.find_order_by_idempotency_key(
            db,
            idempotency_key=delivery.idempotency_key,
        )
        if reconciled_id:
            return _mark_success(db, delivery, order, attempt, reconciled_id, status="reconciled")
        result = connector.create_order(
            db,
            order,
            idempotency_key=delivery.idempotency_key,
        )
        return _mark_success(db, delivery, order, attempt, result.external_order_id)
    except ConnectorDeliveryError as failure:
        _raise_delivery_failure(delivery, order, attempt, failure)
    except (TimeoutError, ConnectionError, OSError) as exc:
        _raise_delivery_failure(
            delivery,
            order,
            attempt,
            ConnectorDeliveryError(str(exc), error_type="connection_error", retryable=True),
        )
    except Exception as exc:  # noqa: BLE001
        _raise_delivery_failure(
            delivery,
            order,
            attempt,
            ConnectorDeliveryError(str(exc), error_type=exc.__class__.__name__),
        )


def retry_order_delivery(db: Session, *, company_id: int, delivery_id: int) -> OrderDelivery | None:
    """Explicitly reopen a failed delivery without creating another outbox intent."""

    delivery = get_order_delivery(db, company_id=company_id, delivery_id=delivery_id)
    if delivery is None or delivery.status == "exported":
        return None
    if delivery.status not in {"failed", "retrying"}:
        return delivery

    order = db.scalar(select(Order).where(Order.id == delivery.order_id, Order.company_id == company_id))
    if order is None:
        return None
    delivery.max_attempts = max(int(delivery.max_attempts or 1), int(delivery.attempt_count or 0) + DEFAULT_MAX_ATTEMPTS)
    delivery.status = "queued"
    delivery.next_attempt_at = _now()
    order.export_status = "ready_to_export"
    order.status = "pedido_validado"

    dedupe_key = delivery_job_dedupe_key(delivery.idempotency_key)
    job = db.scalar(
        select(BackgroundJob).where(
            BackgroundJob.company_id == company_id,
            BackgroundJob.job_type == DELIVERY_JOB_TYPE,
            BackgroundJob.dedupe_key == dedupe_key,
        )
    )
    if job is None:
        enqueue_job(
            db,
            company_id=company_id,
            job_type=DELIVERY_JOB_TYPE,
            payload={"delivery_id": delivery.id},
            dedupe_key=dedupe_key,
            max_retries=DEFAULT_MAX_ATTEMPTS - 1,
        )
    else:
        job.max_retries = max(int(job.max_retries or 0), int(job.attempt_count or 0) + DEFAULT_MAX_ATTEMPTS - 1)
        retry_job(db, company_id, job.id)
    db.commit()
    db.refresh(delivery)
    return delivery
