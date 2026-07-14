from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Email, LLMSettings, Order, OrderLine, ScoringSettings


SAFETY_PRESETS = {
    "conservador": {"safe_threshold": 95, "review_threshold": 82, "doubtful_threshold": 65, "blocked_threshold": 64},
    "equilibrado": {"safe_threshold": 90, "review_threshold": 75, "doubtful_threshold": 50, "blocked_threshold": 49},
    "flexible": {"safe_threshold": 85, "review_threshold": 65, "doubtful_threshold": 40, "blocked_threshold": 39},
}


def apply_safety_level(scoring: ScoringSettings, safety_level: str) -> None:
    preset = SAFETY_PRESETS.get(safety_level)
    if not preset:
        return
    for field, value in preset.items():
        setattr(scoring, field, value)


def agent_status(settings: LLMSettings, metrics: dict) -> dict:
    configured = settings.provider != "disabled" and bool(settings.api_key_encrypted)
    if not settings.agent_enabled or settings.provider == "disabled" or settings.agent_mode == "desactivado":
        level = "inactive"
        label = "Inactivo"
        message = "El agente esta pausado y no procesara correos automaticamente."
    elif not configured:
        level = "error"
        label = "Configuracion incompleta"
        message = "Falta API key o proveedor IA para procesar con LLM real."
    elif settings.last_test_ok is False:
        level = "error"
        label = "Error de conexion"
        message = settings.last_test_message or "La ultima prueba del proveedor IA fallo."
    elif settings.last_test_at is None:
        level = "warning"
        label = "Pendiente de prueba"
        message = "Proveedor configurado, pero aun no se ha validado la conexion."
    else:
        level = "ok"
        label = "Activo"
        message = "Agente configurado y ultima prueba correcta."
    return {
        "level": level,
        "label": label,
        "message": message,
        "configured": configured,
        "connection_label": "Correcta" if settings.last_test_ok else "Sin validar" if settings.last_test_ok is None else "Error",
        "safe_rate": metrics["safe_rate"],
        "review_rate": metrics["review_rate"],
    }


def _period_start(period: str) -> datetime:
    today = date.today()
    days = {"today": 0, "7d": 6, "30d": 29}.get(period, 0)
    return datetime.combine(today - timedelta(days=days), time.min, tzinfo=timezone.utc)


def agent_metrics(db: Session, company_id: int, scoring: ScoringSettings, period: str = "today") -> dict:
    start = _period_start(period)
    emails = db.scalars(select(Email).where(Email.company_id == company_id, Email.received_at >= start)).all()
    orders = db.scalars(
        select(Order)
        .where(Order.company_id == company_id, Order.created_at >= start)
        .options(selectinload(Order.lines), selectinload(Order.email))
    ).unique().all()
    lines = [line for order in orders for line in order.lines]
    generated = len(orders)
    confirmed = len([order for order in orders if order.status in {"confirmed", "pedido_confirmado", "pedido_exportado", "exported"}])
    discarded = len([order for order in orders if order.status in {"discarded", "descartado"}])
    safe = len([order for order in orders if (order.score or 0) >= scoring.safe_threshold])
    review = len([order for order in orders if scoring.review_threshold <= (order.score or 0) < scoring.safe_threshold])
    doubtful = len([order for order in orders if (order.score or 0) < scoring.review_threshold])
    product_missing = len([line for line in lines if not line.validated_product_id and not line.product_id])
    customer_missing = len([order for order in orders if not order.validated_customer_id and not order.customer_id])
    validated_lines = len([line for line in lines if line.validated_product_id or line.product_id])
    avg_score = round(sum((order.score or 0) for order in orders) / generated, 1) if generated else 0
    return {
        "period": period,
        "processed_emails": len([email for email in emails if email.status != "pending"]),
        "detected_orders": len([email for email in emails if email.detected_type == "pedido"]) or generated,
        "no_order_emails": len([email for email in emails if email.detected_type == "no_pedido"]),
        "doubtful_emails": len([email for email in emails if email.detected_type == "dudoso"]),
        "generated_orders": generated,
        "confirmed_without_changes": confirmed,
        "corrected_orders": len([order for order in orders if order.review_reasons]),
        "discarded_orders": discarded,
        "safe_orders": safe,
        "review_orders": review,
        "blocked_orders": doubtful,
        "safe_rate": round((safe * 100 / generated), 1) if generated else 0,
        "review_rate": round(((review + doubtful) * 100 / generated), 1) if generated else 0,
        "validated_line_rate": round((validated_lines * 100 / len(lines)), 1) if lines else 0,
        "product_missing_rate": round((product_missing * 100 / len(lines)), 1) if lines else 0,
        "customer_missing_rate": round((customer_missing * 100 / generated), 1) if generated else 0,
        "avg_score": avg_score,
        "llm_errors": len([email for email in emails if email.status == "error_processing"]),
        "json_errors": 0,
        "pdf_errors": 0,
        "avg_processing_ms": 0,
        "estimated_cost": 0,
    }


def improvement_suggestions(db: Session, company_id: int) -> dict:
    orders = db.scalars(
        select(Order)
        .where(Order.company_id == company_id)
        .options(selectinload(Order.lines), selectinload(Order.email))
        .order_by(Order.created_at.desc())
        .limit(200)
    ).unique().all()
    missing_products: Counter[str] = Counter()
    missing_customers: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for order in orders:
        if not order.customer_id and not order.validated_customer_id:
            key = order.customer_detected_name or (order.email.sender if order.email else "Cliente sin identificar")
            missing_customers[key] += 1
        if order.review_reasons:
            for reason in order.review_reasons.split(";"):
                if reason.strip():
                    errors[reason.strip()] += 1
        for line in order.lines:
            if not line.product_id and not line.validated_product_id:
                key = line.detected_reference or line.detected_product or line.original_text or "Linea sin referencia"
                missing_products[key] += 1
                if line.doubt_reason:
                    errors[line.doubt_reason] += 1
    return {
        "products": [{"text": text, "count": count} for text, count in missing_products.most_common(8)],
        "customers": [{"text": text, "count": count} for text, count in missing_customers.most_common(8)],
        "errors": [{"text": text, "count": count} for text, count in errors.most_common(8)],
    }
