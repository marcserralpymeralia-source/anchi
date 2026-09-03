from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
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
    email_stats = db.execute(
        select(
            func.count(Email.id),
            func.coalesce(func.sum(case((or_(Email.status.is_(None), Email.status != "pending"), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Email.detected_type == "pedido", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Email.detected_type == "no_pedido", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Email.detected_type == "dudoso", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Email.status == "error_processing", 1), else_=0)), 0),
        )
        .where(Email.company_id == company_id, Email.received_at >= start)
    ).one()
    order_stats = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(case((Order.status.in_(("confirmed", "pedido_confirmado", "pedido_exportado", "exported")), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Order.status.in_(("discarded", "descartado")), 1), else_=0)), 0),
            func.coalesce(func.sum(case((func.coalesce(Order.score, 0) >= scoring.safe_threshold, 1), else_=0)), 0),
            func.coalesce(func.sum(case((and_(func.coalesce(Order.score, 0) >= scoring.review_threshold, func.coalesce(Order.score, 0) < scoring.safe_threshold), 1), else_=0)), 0),
            func.coalesce(func.sum(case((func.coalesce(Order.score, 0) < scoring.review_threshold, 1), else_=0)), 0),
            func.coalesce(func.sum(case((and_(Order.review_reasons.is_not(None), Order.review_reasons != ""), 1), else_=0)), 0),
            func.coalesce(func.sum(case((and_(Order.validated_customer_id.is_(None), Order.customer_id.is_(None)), 1), else_=0)), 0),
            func.coalesce(func.sum(func.coalesce(Order.score, 0)), 0),
        )
        .where(Order.company_id == company_id, Order.created_at >= start)
    ).one()
    line_stats = db.execute(
        select(
            func.count(OrderLine.id),
            func.coalesce(func.sum(case((and_(OrderLine.validated_product_id.is_(None), OrderLine.product_id.is_(None)), 1), else_=0)), 0),
            func.coalesce(func.sum(case((or_(OrderLine.validated_product_id.is_not(None), OrderLine.product_id.is_not(None)), 1), else_=0)), 0),
        )
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.company_id == company_id, Order.created_at >= start)
    ).one()
    _, processed_emails, detected_orders, no_order_emails, doubtful_emails, llm_errors = email_stats
    generated, confirmed, discarded, safe, review, doubtful, corrected, customer_missing, score_total = order_stats
    line_count, product_missing, validated_lines = line_stats
    avg_score = round(float(score_total or 0) / generated, 1) if generated else 0
    return {
        "period": period,
        "processed_emails": int(processed_emails or 0),
        "detected_orders": int(detected_orders or 0) or int(generated or 0),
        "no_order_emails": int(no_order_emails or 0),
        "doubtful_emails": int(doubtful_emails or 0),
        "generated_orders": int(generated or 0),
        "confirmed_without_changes": int(confirmed or 0),
        "corrected_orders": int(corrected or 0),
        "discarded_orders": int(discarded or 0),
        "safe_orders": int(safe or 0),
        "review_orders": int(review or 0),
        "blocked_orders": int(doubtful or 0),
        "safe_rate": round((safe * 100 / generated), 1) if generated else 0,
        "review_rate": round(((review + doubtful) * 100 / generated), 1) if generated else 0,
        "validated_line_rate": round((validated_lines * 100 / line_count), 1) if line_count else 0,
        "product_missing_rate": round((product_missing * 100 / line_count), 1) if line_count else 0,
        "customer_missing_rate": round((customer_missing * 100 / generated), 1) if generated else 0,
        "avg_score": avg_score,
        "llm_errors": int(llm_errors or 0),
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
