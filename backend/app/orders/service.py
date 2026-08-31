from __future__ import annotations

import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parseaddr
from types import SimpleNamespace

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.agent.platform import LearningService
from app.agent.services import ScoringService
from app.db.models import Customer, CustomerAlias, CustomerContactPoint, CustomerDomain, Email, EmailAttachment, ManualCorrection, Order, OrderLine, Product, ProductAlias, ScoringSettings
from app.logs.service import log_action
from app.master.service import TenantUser
from app.orders.state import ORDER_STATE, PENDING_ORDER_STATUSES, REVIEW_ORDER_STATUSES


_DELETE_ROLES = {"Administrador", "Superadmin", "Owner", "Propietario"}


def order_score_category(score: float | None, scoring: ScoringSettings) -> tuple[str, str]:
    category = ORDER_STATE.scoring_category(score, scoring)
    return {
        "without_score": ("without_score", "Sin scoring"),
        "safe": ("safe", "Seguro"),
        "reviewable": ("reviewable", "Revisable"),
        "doubtful": ("doubtful", "Dudoso"),
        "not_importable": ("not_importable", "No importable"),
    }.get(category, (category, category.title()))


def order_alerts(
    order: Order,
    scoring: ScoringSettings,
    *,
    line_count: int | None = None,
    doubtful_count: int | None = None,
    has_pdf: bool | None = None,
) -> dict:
    if line_count is None or doubtful_count is None or has_pdf is None:
        lines = order.lines or []
        doubtful_lines = [line for line in lines if line.validation_status != "validated" or not line.validated_product_id or line.doubt_reason]
        line_count = len(lines)
        doubtful_count = len(doubtful_lines)
        has_pdf = bool(order.email and any(attachment.content_type == "application/pdf" for attachment in order.email.attachments))
    return {
        "line_count": int(line_count or 0),
        "doubtful_count": int(doubtful_count or 0),
        "has_pdf": bool(has_pdf),
        "has_notes": bool(order.notes),
        "requires_review": bool((doubtful_count or 0) or order.status in PENDING_ORDER_STATUSES | REVIEW_ORDER_STATUSES),
    }


def validate_confirmation(order: Order, scoring: ScoringSettings) -> list[str]:
    errors = ORDER_STATE.validate_blockers(order, scoring)
    if scoring.block_without_customer and not order.validated_customer_id:
        errors = ["No se puede confirmar porque no hay cliente validado.", *errors]
    if scoring.block_without_reference and any(not line.validated_product_id for line in order.lines):
        errors = ["No se puede confirmar porque hay lineas sin referencia validada.", *errors]
    if scoring.block_without_quantity and any(line.quantity is None or line.quantity <= 0 for line in order.lines):
        errors = ["No se puede confirmar porque hay lineas sin cantidad valida.", *errors]
    if scoring.block_below_threshold and order.score < scoring.doubtful_threshold:
        errors = ["No se puede confirmar porque el scoring esta por debajo del umbral minimo.", *errors]
    return list(dict.fromkeys(errors))


def _customer_label(order: Order) -> str:
    customer = order.validated_customer or order.customer
    return f"{customer.code} · {customer.fiscal_name}" if customer else "Sin cliente"


def _candidate_terms(*values: str | None) -> list[str]:
    terms: list[str] = []
    for value in values:
        cleaned = " ".join((value or "").strip().split())
        if cleaned and cleaned not in terms:
            terms.append(cleaned)
    return terms


def _normalize_match_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "--"


def _review_customer_snapshot(db: Session, order: Order, customer: Customer | None) -> dict:
    if not customer:
        return {"identified": False}
    last_order_at = db.scalar(
        select(func.max(Order.created_at)).where(
            Order.company_id == customer.company_id,
            or_(Order.customer_id == customer.id, Order.validated_customer_id == customer.id),
            Order.deleted_at.is_(None),
        )
    )
    return {
        "identified": True,
        "customer": {
            "id": customer.id,
            "code": customer.code,
            "name": customer.fiscal_name,
            "commercial_name": customer.commercial_name or "",
            "email": customer.primary_email or "",
            "phone": customer.phone or "",
            "delegation": customer.delegation or "",
            "status": customer.status or "active",
            "confidence": order.customer_score or 0,
            "primary_endpoint": customer.primary_email or customer.phone or "",
            "habitual_channel": "Email" if customer.primary_email else ("Teléfono" if customer.phone else "Sin dato"),
            "last_order_at": _fmt_dt(last_order_at),
            "knowledge_url": f"/customers/{customer.id}/knowledge",
        },
    }


def _review_product_candidates(
    db: Session,
    *,
    company_id: int,
    order: Order,
    query: str = "",
    limit: int = 12,
) -> list[Product]:
    current_ids = {
        value
        for value in {
            *(line.product_id for line in (order.lines or [])),
            *(line.validated_product_id for line in (order.lines or [])),
        }
        if value
    }
    stmt = select(Product).where(Product.company_id == company_id, Product.deleted_at.is_(None))
    match_conditions = []
    if query.strip():
        terms = _candidate_terms(query)
    else:
        terms = _candidate_terms(
            order.customer_detected_name,
            order.email.sender if order.email else None,
            order.email.subject if order.email else None,
            *(line.detected_reference for line in (order.lines or [])),
            *(line.detected_product for line in (order.lines or [])),
            *(line.original_text for line in (order.lines or [])),
        )
    if terms:
        for term in terms[:6]:
            match_conditions.append(
                or_(
                    Product.reference.ilike(f"%{term}%"),
                    Product.alternative_code.ilike(f"%{term}%"),
                    Product.name.ilike(f"%{term}%"),
                    Product.description.ilike(f"%{term}%"),
                    Product.family.ilike(f"%{term}%"),
                    Product.subfamily.ilike(f"%{term}%"),
                    Product.ean.ilike(f"%{term}%"),
                    exists().where(ProductAlias.company_id == company_id, ProductAlias.product_id == Product.id, ProductAlias.alias.ilike(f"%{term}%")),
                )
            )
    if current_ids and match_conditions:
        stmt = stmt.where(or_(Product.id.in_(list(current_ids)), *match_conditions))
    elif current_ids:
        stmt = stmt.where(Product.id.in_(list(current_ids)))
    elif match_conditions:
        stmt = stmt.where(or_(*match_conditions))
    stmt = stmt.options(
        load_only(
            Product.id,
            Product.reference,
            Product.alternative_code,
            Product.name,
            Product.family,
            Product.subfamily,
            Product.ean,
        ),
        selectinload(Product.aliases).load_only(ProductAlias.id, ProductAlias.alias),
    )
    order_by_clauses = []
    if current_ids:
        order_by_clauses.append(case((Product.id.in_(list(current_ids)), 0), else_=1))
    order_by_clauses.append(Product.reference.asc())
    stmt = stmt.order_by(*order_by_clauses).limit(limit)
    return db.scalars(stmt).all()


def _product_suggestions_for_line(products: list[Product], line: OrderLine, limit: int = 3) -> list[dict]:
    if not products:
        return []
    query_parts = [
        line.validated_product.reference if line.validated_product else "",
        line.detected_reference or "",
        line.detected_product or "",
        line.original_text or "",
    ]
    query = _normalize_match_text(" ".join(part for part in query_parts if part))
    if not query:
        return []
    suggestions: list[dict] = []
    for product in products:
        score = 0.0
        reference = _normalize_match_text(product.reference)
        name = _normalize_match_text(product.name)
        aliases = [_normalize_match_text(alias.alias) for alias in (product.aliases or [])]
        if reference and reference == query:
            score = 1.0
        else:
            if reference and (reference in query or query in reference):
                score = max(score, 0.96 if reference == query else 0.84)
            if name:
                score = max(score, SequenceMatcher(None, query, name).ratio())
            for alias in aliases:
                if not alias:
                    continue
                if alias == query:
                    score = max(score, 0.98)
                else:
                    score = max(score, SequenceMatcher(None, query, alias).ratio())
                if alias in query or query in alias:
                    score = max(score, 0.9)
        if score >= 0.45:
            suggestions.append(
                {
                    "id": product.id,
                    "label": f"{product.reference} · {product.name}",
                    "score": round(score * 100, 1),
                }
            )
    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return suggestions[:limit]


def _can_delete(user: TenantUser) -> bool:
    return user.role.name in _DELETE_ROLES


def _soft_delete_order(db: Session, order: Order, user: TenantUser, reason: str | None = None) -> None:
    order.status = "deleted"
    order.deleted_at = datetime.now(timezone.utc)
    order.deleted_by = user.id
    order.delete_reason = reason or "Eliminado desde la app"
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="order.delete", entity_type="order", entity_id=order.id, message=reason or "Pedido eliminado")


def sync_customer_product_knowledge(
    db: Session,
    *,
    company_id: int,
    order: Order,
    line: OrderLine,
    user: TenantUser,
    source_context: str,
    is_manual: bool = False,
    exported_at: datetime | None = None,
    delivery_note_at: datetime | None = None,
    force_habitual: bool = False,
) -> None:
    learning = LearningService()
    customer = order.validated_customer or order.customer
    product = line.validated_product or line.product
    if not customer or not product:
        learning.record_knowledge_conflict(
            db,
            company_id=company_id,
            customer_id=customer.id if customer else None,
            product_id=product.id if product else None,
            title="Línea sin relación valida",
            message="No se pudo consolidar el conocimiento cliente-producto porque faltan datos validados.",
            payload={"order_id": order.id, "line_id": line.id, "source_context": source_context},
            order_id=order.id,
        )
        return
    learning.update_customer_product_knowledge(
        db,
        company_id=company_id,
        customer=customer,
        product=product,
        quantity=line.quantity,
        unit=line.unit,
        order=order,
        source_context=source_context,
        customer_alias_used=order.customer_detected_name,
        comments=order.notes or line.original_text,
        is_manual=is_manual,
        exported_at=exported_at,
        delivery_note_at=delivery_note_at,
        force_habitual=force_habitual,
    )


_sync_customer_product_knowledge = sync_customer_product_knowledge


def learn_customer_email_from_confirmed_order(
    db: Session,
    *,
    order: Order,
    company_id: int,
) -> str:
    customer_id = order.validated_customer_id or order.customer_id
    if not customer_id or not order.email_id:
        return "skipped"

    email = db.get(Email, order.email_id)
    if not email:
        return "skipped"

    sender_email = parseaddr(email.sender or "")[1].strip().lower()
    if not sender_email or "@" not in sender_email:
        return "skipped"

    existing_points = db.scalars(
        select(CustomerContactPoint).where(
            CustomerContactPoint.company_id == company_id,
            CustomerContactPoint.type == "email",
            CustomerContactPoint.value == sender_email,
            CustomerContactPoint.active == True,  # noqa: E712
        )
    ).all()

    existing_customer_ids = {point.customer_id for point in existing_points}
    if existing_customer_ids and existing_customer_ids != {customer_id}:
        return "conflict"

    now = datetime.now(timezone.utc)

    if existing_points:
        for point in existing_points:
            point.confidence = 1.0
            point.source = "validated_order"
            point.last_seen_at = now
            point.updated_at = now
            point.active = True
        return "updated"

    db.add(
        CustomerContactPoint(
            company_id=company_id,
            customer_id=customer_id,
            type="email",
            value=sender_email,
            label="Email aprendido desde pedido validado",
            is_primary=False,
            active=True,
            confidence=1.0,
            source="validated_order",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    return "created"


def confirm_order_with_effects(
    db: Session,
    *,
    order: Order,
    company_id: int,
    scoring: ScoringSettings,
    user=None,
    source_context: str = "pedido_confirmado",
    when: datetime | None = None,
) -> dict:
    errors = validate_confirmation(order, scoring)
    if errors:
        return {
            "confirmed": False,
            "errors": errors,
            "email_learning_result": "skipped",
        }

    ORDER_STATE.confirm(order, when=when or datetime.now(timezone.utc))

    for line in order.lines or []:
        sync_customer_product_knowledge(
            db,
            company_id=company_id,
            order=order,
            line=line,
            user=user,
            source_context=source_context,
            force_habitual=False,
        )

    email_learning_result = learn_customer_email_from_confirmed_order(
        db,
        order=order,
        company_id=company_id,
    )

    LearningService().record_case(
        db,
        company_id=company_id,
        summary=f"{_customer_label(order)} confirmado con {len(order.lines or [])} lineas.",
        resolved_action="pedido_confirmado",
        resolution_json=json.dumps(
            {
                "order_id": order.id,
                "customer": _customer_label(order),
                "lines": len(order.lines or []),
            },
            ensure_ascii=False,
        ),
        customer_id=order.validated_customer_id or order.customer_id,
        order_id=order.id,
    )

    return {
        "confirmed": True,
        "errors": [],
        "email_learning_result": email_learning_result,
    }
