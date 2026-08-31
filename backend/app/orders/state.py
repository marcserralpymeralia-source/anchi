from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Order, OrderLine, ScoringSettings
from app.orders.scoring import is_positive_quantity
from app.settings.service import get_or_create_settings


PENDING_ORDER_STATUSES = {"pedido_pendiente_revision", "pending_review"}
CONFIRMED_ORDER_STATUSES = {"pedido_confirmado", "pedido_validado"}
EXPORT_ORDER_STATUSES = {"pedido_exportado"}
REVIEW_ORDER_STATUSES = {"dudoso", "no_importable"}
ERROR_ORDER_STATUSES = {"error_exportacion", "error_procesamiento"}
TERMINAL_ORDER_STATUSES = {
    "pedido_confirmado",
    "pedido_validado",
    "pedido_exportado",
    "cerrado",
    "cancelado",
    "descartado",
    "deleted",
    "archived_deleted",
}


@dataclass(slots=True)
class OrderStateService:
    def scoring_category(self, score: float | None, settings: ScoringSettings) -> str:
        if score is None:
            return "without_score"
        if score >= settings.safe_threshold:
            return "safe"
        if score >= settings.review_threshold:
            return "reviewable"
        if score >= settings.doubtful_threshold:
            return "doubtful"
        return "not_importable"

    def line_metrics(self, order: Order, line_metrics: dict[int, dict[str, int]] | None = None) -> dict[str, int]:
        if line_metrics and order.id is not None and order.id in line_metrics:
            metrics = line_metrics[order.id]
            return {
                "line_count": int(metrics.get("line_count", 0) or 0),
                "doubt_count": int(metrics.get("doubt_count", 0) or 0),
                "missing_product_count": int(metrics.get("missing_product_count", 0) or 0),
                "invalid_quantity_count": int(metrics.get("invalid_quantity_count", 0) or 0),
            }
        lines = order.lines or []
        return {
            "line_count": len(lines),
            "doubt_count": sum(1 for line in lines if line.validation_status != "validated" or not line.validated_product_id or line.doubt_reason),
            "missing_product_count": sum(1 for line in lines if not line.validated_product_id),
            "invalid_quantity_count": sum(1 for line in lines if not is_positive_quantity(line.quantity)),
        }

    def validate_blockers(self, order: Order, settings: ScoringSettings, *, line_metrics: dict[int, dict[str, int]] | None = None) -> list[str]:
        blockers: list[str] = []
        metrics = self.line_metrics(order, line_metrics)
        if settings.block_without_customer and not order.validated_customer_id:
            blockers.append("Cliente no identificado")
        if settings.block_without_reference and metrics["missing_product_count"] > 0:
            blockers.append("Productos sin referencia")
        if settings.block_without_quantity and metrics["invalid_quantity_count"] > 0:
            blockers.append("Cantidad dudosa")
        if settings.block_below_threshold and (order.score is None or order.score < settings.doubtful_threshold):
            blockers.append("Confianza baja")
        return blockers

    def operational_state(self, order: Order, settings: ScoringSettings, *, line_metrics: dict[int, dict[str, int]] | None = None) -> str:
        if order.status in ERROR_ORDER_STATUSES:
            return "error"
        if self.validate_blockers(order, settings, line_metrics=line_metrics):
            return "blocked"
        category = self.scoring_category(order.score, settings)
        if category == "safe" and order.status in PENDING_ORDER_STATUSES:
            return "ready"
        if category in {"reviewable", "doubtful"} or order.status in REVIEW_ORDER_STATUSES:
            return "review"
        if order.status in EXPORT_ORDER_STATUSES:
            return "exported"
        return "normal"

    def requires_review(self, order: Order, settings: ScoringSettings, *, line_metrics: dict[int, dict[str, int]] | None = None) -> bool:
        return bool(self.validate_blockers(order, settings, line_metrics=line_metrics) or order.status in PENDING_ORDER_STATUSES | REVIEW_ORDER_STATUSES)

    def is_terminal(self, status: str | None) -> bool:
        return bool(status and status in TERMINAL_ORDER_STATUSES)

    def status_for_score(self, db: Session, company_id: int, score: float) -> str:
        settings = get_or_create_settings(db, ScoringSettings, company_id)
        if score >= settings.safe_threshold:
            return "pedido_pendiente_revision"
        if score >= settings.review_threshold:
            return "pedido_pendiente_revision"
        if score >= settings.doubtful_threshold:
            return "dudoso"
        return "no_importable"

    def apply_score(self, db: Session, order: Order, company_id: int, score: float) -> str:
        order.score = score
        order.status = self.status_for_score(db, company_id, score)
        return order.status

    def confirm(self, order: Order, *, when=None) -> None:  # noqa: ANN001
        order.status = "pedido_confirmado"
        order.confirmed_at = when

    def export(self, order: Order, *, ok: bool, when=None) -> None:  # noqa: ANN001
        order.status = "pedido_exportado" if ok else "error_exportacion"
        if ok and when is not None:
            order.exported_at = order.exported_at or when

    def mark_no_order(self, order: Order) -> None:
        order.status = "no_pedido"

    def discard(self, order: Order) -> None:
        order.status = "descartado"

    def change_state(self, order: Order, target_state: str) -> None:
        order.status = target_state


ORDER_STATE = OrderStateService()
