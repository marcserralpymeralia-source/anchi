import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.core.templating import templates
from app.agent.platform import LearningService
from app.agent.services import MockAgentService, ScoringService
from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.pagination import paginate
from app.db.models import Customer, CustomerAlias, CustomerContactPoint, CustomerDomain, Email, EmailAttachment, ExportFile, FTPSettings, ManualCorrection, Order, OrderLine, Product, ProductAlias, RagCase, ScoringSettings, User, utcnow
from app.databases.service import build_customer_context
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/orders", tags=["orders"])


_DELETE_ROLES = {"Administrador", "Superadmin", "Owner", "Propietario"}


def order_score_category(score: float | None, scoring: ScoringSettings) -> tuple[str, str]:
    if score is None:
        return "without_score", "Sin scoring"
    if score >= scoring.safe_threshold:
        return "safe", "Seguro"
    if score >= scoring.review_threshold:
        return "reviewable", "Revisable"
    if score >= scoring.doubtful_threshold:
        return "doubtful", "Dudoso"
    return "not_importable", "No importable"


def order_alerts(order: Order, *, line_count: int | None = None, doubtful_count: int | None = None, has_pdf: bool | None = None) -> dict:
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
        "requires_review": bool((doubtful_count or 0) or order.status in {"pedido_pendiente_revision", "pending_review", "dudoso", "no_importable"}),
    }


def validate_confirmation(order: Order, scoring: ScoringSettings) -> list[str]:
    errors: list[str] = []
    if scoring.block_without_customer and not order.validated_customer_id:
        errors.append("No se puede confirmar porque no hay cliente validado.")
    if scoring.block_without_reference and any(not line.validated_product_id for line in order.lines):
        errors.append("No se puede confirmar porque hay lineas sin referencia validada.")
    if scoring.block_without_quantity and any(line.quantity is None or line.quantity <= 0 for line in order.lines):
        errors.append("No se puede confirmar porque hay lineas sin cantidad valida.")
    if scoring.block_below_threshold and order.score < scoring.doubtful_threshold:
        errors.append("No se puede confirmar porque el scoring esta por debajo del umbral minimo.")
    return errors


def _customer_label(order: Order) -> str:
    customer = order.validated_customer or order.customer
    return f"{customer.code} · {customer.fiscal_name}" if customer else "Sin cliente"


def _product_label(line: OrderLine) -> str:
    product = line.validated_product or line.product
    return f"{product.reference} · {product.name}" if product else "Sin producto"


def _normalize_match_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


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


def _sync_customer_product_knowledge(
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


@router.get("")
def list_orders(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    customer_id: int = 0,
    score_min: str = "",
    score_max: str = "",
    status: str = "",
    email_type: str = "",
    sender: str = "",
    search: str = "",
    scoring_category: str = "",
    has_pdf: str = "",
    requires_review: str = "",
    sort: str = "date_desc",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    stmt = select(Order).where(Order.company_id == user.company_id, Order.deleted_at.is_(None))
    if date_from:
        stmt = stmt.where(Order.created_at >= datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(Order.created_at <= datetime.fromisoformat(f"{date_to}T23:59:59").replace(tzinfo=timezone.utc))
    if customer_id:
        stmt = stmt.where((Order.customer_id == customer_id) | (Order.validated_customer_id == customer_id))
    parsed_score_min = float(score_min) if score_min else None
    parsed_score_max = float(score_max) if score_max else None
    if parsed_score_min is not None:
        stmt = stmt.where(Order.score >= parsed_score_min)
    if parsed_score_max is not None:
        stmt = stmt.where(Order.score <= parsed_score_max)
    if status:
        stmt = stmt.where(Order.status == status)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    if scoring_category:
        if scoring_category == "safe":
            stmt = stmt.where(Order.score >= scoring.safe_threshold)
        elif scoring_category == "reviewable":
            stmt = stmt.where(Order.score >= scoring.review_threshold, Order.score < scoring.safe_threshold)
        elif scoring_category == "doubtful":
            stmt = stmt.where(Order.score >= scoring.doubtful_threshold, Order.score < scoring.review_threshold)
        elif scoring_category == "not_importable":
            stmt = stmt.where(Order.score < scoring.doubtful_threshold)
        elif scoring_category == "without_score":
            stmt = stmt.where(Order.score.is_(None))
    if has_pdf:
        pdf_exists = exists().where(EmailAttachment.email_id == Order.email_id, EmailAttachment.content_type == "application/pdf")
        stmt = stmt.where(pdf_exists if has_pdf == "yes" else ~pdf_exists)
    if requires_review:
        if requires_review == "yes":
            stmt = stmt.where(or_(Order.status.in_(["pedido_pendiente_revision", "pending_review", "dudoso", "no_importable"]), Order.score < scoring.safe_threshold))
        else:
            stmt = stmt.where(Order.status.in_(["pedido_confirmado", "pedido_exportado"]))
    joined_email = False
    if email_type:
        stmt = stmt.join(Email, Order.email_id == Email.id).where(Email.detected_type == email_type)
        joined_email = True
    if sender:
        if not joined_email:
            stmt = stmt.join(Email, Order.email_id == Email.id)
            joined_email = True
        stmt = stmt.where(Email.sender.ilike(f"%{sender}%"))
    if search:
        if not joined_email:
            stmt = stmt.join(Email, Order.email_id == Email.id)
            joined_email = True
        like = f"%{search}%"
        stmt = stmt.where(or_(Email.subject.ilike(like), Order.customer_detected_name.ilike(like)))
    sort_map = {
        "date_asc": Order.created_at.asc(),
        "date_desc": Order.created_at.desc(),
        "score_asc": Order.score.asc(),
        "score_desc": Order.score.desc(),
        "status_asc": Order.status.asc(),
        "customer_asc": Order.customer_detected_name.asc(),
    }
    stmt = stmt.options(
        load_only(
            Order.id,
            Order.email_id,
            Order.customer_id,
            Order.validated_customer_id,
            Order.customer_detected_name,
            Order.score,
            Order.status,
            Order.notes,
            Order.created_at,
        ),
        selectinload(Order.email).load_only(Email.id, Email.sender, Email.subject, Email.detected_type, Email.received_at, Email.external_id),
        selectinload(Order.customer).load_only(Customer.id, Customer.code, Customer.fiscal_name),
        selectinload(Order.validated_customer).load_only(Customer.id, Customer.code, Customer.fiscal_name),
    ).order_by(sort_map.get(sort, Order.created_at.desc()))
    orders, pagination = paginate(db, stmt, page=page, page_size=page_size)
    customers = tuple(db.scalars(select(Customer).where(Customer.company_id == user.company_id, Customer.deleted_at.is_(None)).order_by(Customer.fiscal_name)).all())
    statuses = tuple(db.scalars(select(Order.status).where(Order.company_id == user.company_id).distinct().order_by(Order.status)).all())
    order_ids = [order.id for order in orders]
    line_metrics: dict[int, dict[str, int]] = {}
    pdf_flags: dict[int, bool] = {}
    if order_ids:
        doubtful_case = case(
            (
                or_(
                    OrderLine.validation_status != "validated",
                    OrderLine.validated_product_id.is_(None),
                    OrderLine.doubt_reason.is_not(None),
                ),
                1,
            ),
            else_=0,
        )
        for order_id, line_count, doubtful_count in db.execute(
            select(
                OrderLine.order_id,
                func.count(OrderLine.id).label("line_count"),
                func.coalesce(func.sum(doubtful_case), 0).label("doubtful_count"),
            )
            .where(OrderLine.order_id.in_(order_ids))
            .group_by(OrderLine.order_id)
        ):
            line_metrics[int(order_id)] = {
                "line_count": int(line_count or 0),
                "doubtful_count": int(doubtful_count or 0),
            }
        for order_id, pdf_count in db.execute(
            select(
                Order.id,
                func.count(EmailAttachment.id).label("pdf_count"),
            )
            .join(Email, Order.email_id == Email.id)
            .join(EmailAttachment, EmailAttachment.email_id == Email.id)
            .where(Order.id.in_(order_ids), EmailAttachment.content_type == "application/pdf")
            .group_by(Order.id)
        ):
            pdf_flags[int(order_id)] = bool(pdf_count)
    filters = {"date_from": date_from, "date_to": date_to, "customer_id": customer_id, "score_min": score_min, "score_max": score_max, "status": status, "email_type": email_type, "sender": sender, "search": search, "scoring_category": scoring_category, "has_pdf": has_pdf, "requires_review": requires_review, "sort": sort}
    categories = {order.id: order_score_category(order.score, scoring) for order in orders}
    alerts = {
        order.id: order_alerts(
            order,
            line_count=line_metrics.get(order.id, {}).get("line_count", 0),
            doubtful_count=line_metrics.get(order.id, {}).get("doubtful_count", 0),
            has_pdf=pdf_flags.get(order.id, False),
        )
        for order in orders
    }
    return templates.TemplateResponse("orders/list.html", {"request": request, "user": user, "orders": orders, "customers": customers, "statuses": statuses, "pagination": pagination, "filters": filters, "scoring": scoring, "categories": categories, "alerts": alerts})


@router.post("/mock")
def create_mock_order(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = MockAgentService().create_mock_order(db, user.company_id)
    log_action(db, company_id=user.company_id, user=user, action="agent.mock_order", entity_type="order", entity_id=order.id, message="Pedido mock creado")
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


@router.get("/{order_id}")
def order_detail(order_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.company_id == user.company_id)
        .options(selectinload(Order.lines).selectinload(OrderLine.product), selectinload(Order.lines).selectinload(OrderLine.validated_product), selectinload(Order.email).selectinload(Email.attachments), selectinload(Order.customer), selectinload(Order.validated_customer))
    )
    products = db.scalars(select(Product).where(Product.company_id == user.company_id).options(selectinload(Product.aliases)).order_by(Product.reference)).all()
    customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name)).all()
    exports = db.scalars(select(ExportFile).where(ExportFile.order_id == order_id).order_by(ExportFile.created_at.desc())).all()
    customer_context = build_customer_context(db, user.company_id, (order.validated_customer_id or order.customer_id) if order else None, order=order)
    line_suggestions = {line.id: _product_suggestions_for_line(products, line) for line in (order.lines or [])}
    return templates.TemplateResponse("orders/detail.html", {"request": request, "user": user, "order": order, "products": products, "customers": customers, "exports": exports, "customer_context": customer_context, "line_suggestions": line_suggestions})


@router.post("/{order_id}/customer")
def update_order_customer(order_id: int, validated_customer_id: int = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        previous_customer = order.validated_customer or order.customer
        order.validated_customer_id = validated_customer_id
        order.customer_id = validated_customer_id
        new_customer = db.get(Customer, validated_customer_id)
        if new_customer and (not previous_customer or previous_customer.id != new_customer.id):
            db.add(
                ManualCorrection(
                    company_id=user.company_id,
                    order_id=order.id,
                    entity_type="customer",
                    field_name="validated_customer_id",
                    original_value=f"{previous_customer.code} · {previous_customer.fiscal_name}" if previous_customer else None,
                    corrected_value=f"{new_customer.code} · {new_customer.fiscal_name}",
                    agent_value=order.customer_detected_name or order.notes,
                    corrected_entity_id=new_customer.id,
                    reason="Validacion manual de cliente",
                    should_learn=True,
                    created_by_user_id=user.id,
                )
            )
        if new_customer:
            learning = LearningService()
            for line in order.lines:
                _sync_customer_product_knowledge(
                    db,
                    company_id=user.company_id,
                    order=order,
                    line=line,
                    user=user,
                    source_context="correccion_cliente",
                    is_manual=True,
                )
        order.score = ScoringService().score_order(db, order)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.customer.update", entity_type="order", entity_id=order.id, message="Cliente validado actualizado")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/update")
def update_order(
    order_id: int,
    validated_customer_id: int = Form(0),
    order_date: str = Form(""),
    requested_delivery_date: str = Form(""),
    notes: str = Form(""),
    status: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        previous_notes = order.notes or ""
        previous_customer_id = order.validated_customer_id or order.customer_id
        order.validated_customer_id = validated_customer_id or None
        order.customer_id = validated_customer_id or order.customer_id
        order.order_date = order_date
        order.requested_delivery_date = requested_delivery_date
        order.notes = notes
        if status:
            order.status = status
        if validated_customer_id and validated_customer_id != previous_customer_id:
            new_customer = db.get(Customer, validated_customer_id)
            old_customer = db.get(Customer, previous_customer_id) if previous_customer_id else None
            if new_customer:
                db.add(
                    ManualCorrection(
                        company_id=user.company_id,
                        order_id=order.id,
                        entity_type="customer",
                        field_name="validated_customer_id",
                        original_value=f"{old_customer.code} · {old_customer.fiscal_name}" if old_customer else None,
                        corrected_value=f"{new_customer.code} · {new_customer.fiscal_name}",
                        agent_value=previous_notes or order.customer_detected_name,
                        corrected_entity_id=new_customer.id,
                        reason="Cambio manual desde detalle de pedido",
                        should_learn=True,
                        created_by_user_id=user.id,
                    )
                )
            if new_customer:
                for line in order.lines:
                    _sync_customer_product_knowledge(
                        db,
                        company_id=user.company_id,
                        order=order,
                        line=line,
                        user=user,
                        source_context="correccion_cliente",
                        is_manual=True,
                    )
        order.score = ScoringService().score_order(db, order)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.update", entity_type="order", entity_id=order.id, message="Pedido actualizado")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/lines/{line_id}")
def update_order_line(
    order_id: int,
    line_id: int,
    validated_product_id: int = Form(0),
    quantity: float = Form(...),
    unit: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    line = db.get(OrderLine, line_id)
    order = db.get(Order, order_id)
    if line and order and line.company_id == user.company_id and order.company_id == user.company_id:
        old_product = line.validated_product or line.product
        old_quantity = line.quantity
        old_unit = line.unit
        line.validated_product_id = validated_product_id or None
        line.product_id = validated_product_id or line.product_id
        line.quantity = quantity
        line.unit = unit
        line.validation_status = "validated" if line.validated_product_id and quantity else "pending"
        line.doubt_reason = "" if line.validation_status == "validated" else "Linea pendiente de validar"
        new_product = db.get(Product, validated_product_id) if validated_product_id else None
        if new_product and (not old_product or old_product.id != new_product.id):
            db.add(
                ManualCorrection(
                    company_id=user.company_id,
                    order_id=order.id,
                    order_line_id=line.id,
                    entity_type="product",
                    field_name="validated_product_id",
                    original_value=f"{old_product.reference} · {old_product.name}" if old_product else line.detected_product,
                    corrected_value=f"{new_product.reference} · {new_product.name}",
                    agent_value=line.detected_product or line.original_text,
                    corrected_entity_id=new_product.id,
                    reason="Validacion manual de producto",
                    should_learn=True,
                    created_by_user_id=user.id,
                )
            )
            if old_product and (order.validated_customer_id or order.customer_id):
                LearningService().penalize_customer_product_knowledge(
                    db,
                    company_id=user.company_id,
                    customer_id=order.validated_customer_id or order.customer_id,
                    product_id=old_product.id,
                    reason="El producto propuesto fue corregido manualmente por el usuario.",
                    order_id=order.id,
                )
        if old_quantity != quantity:
            db.add(
                ManualCorrection(
                    company_id=user.company_id,
                    order_id=order.id,
                    order_line_id=line.id,
                    entity_type="quantity",
                    field_name="quantity",
                    original_value=str(old_quantity) if old_quantity is not None else None,
                    corrected_value=str(quantity),
                    agent_value=str(old_quantity) if old_quantity is not None else None,
                    corrected_entity_id=line.id,
                    reason="Correccion manual de cantidad",
                    should_learn=True,
                    created_by_user_id=user.id,
                )
            )
        if old_unit != unit:
            db.add(
                ManualCorrection(
                    company_id=user.company_id,
                    order_id=order.id,
                    order_line_id=line.id,
                    entity_type="unit",
                    field_name="unit",
                    original_value=old_unit,
                    corrected_value=unit,
                    agent_value=old_unit,
                    corrected_entity_id=line.id,
                    reason="Correccion manual de unidad",
                    should_learn=True,
                    created_by_user_id=user.id,
                )
            )
        _sync_customer_product_knowledge(
            db,
            company_id=user.company_id,
            order=order,
            line=line,
            user=user,
            source_context="correccion_linea",
            is_manual=bool(new_product or old_quantity != quantity or old_unit != unit),
        )
        order.score = ScoringService().score_order(db, order)
        order.status = ScoringService().status_for_score(db, user.company_id, order.score)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.line.update", entity_type="order_line", entity_id=line.id, message="Linea de pedido actualizada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/lines")
def add_order_line(
    order_id: int,
    validated_product_id: int = Form(0),
    original_text: str = Form("Linea manual"),
    quantity: float = Form(1),
    unit: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    order = db.get(Order, order_id)
    product = db.get(Product, validated_product_id) if validated_product_id else None
    if order and order.company_id == user.company_id:
        line = OrderLine(
            company_id=user.company_id,
            order_id=order.id,
            product_id=product.id if product else None,
            validated_product_id=product.id if product else None,
            original_text=original_text,
            detected_product=product.name if product else "",
            detected_reference=product.reference if product else "",
            quantity=quantity,
            unit=unit,
            extraction_confidence=1,
            line_score=100 if product else 30,
            validation_status="validated" if product else "pending",
            doubt_reason="" if product else "Linea manual sin referencia validada",
        )
        db.add(line)
        db.flush()
        _sync_customer_product_knowledge(
            db,
            company_id=user.company_id,
            order=order,
            line=line,
            user=user,
            source_context="linea_manual",
            is_manual=True,
            force_habitual=bool(product),
        )
        order.score = ScoringService().score_order(db, order)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.line.add", entity_type="order_line", entity_id=line.id, message="Linea de pedido anadida")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/lines/{line_id}/duplicate")
def duplicate_order_line(order_id: int, line_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    line = db.get(OrderLine, line_id)
    order = db.get(Order, order_id)
    if line and order and line.company_id == user.company_id and order.company_id == user.company_id:
        new_line = OrderLine(
            company_id=user.company_id,
            order_id=order.id,
            product_id=line.product_id,
            validated_product_id=line.validated_product_id,
            original_text=line.original_text,
            detected_reference=line.detected_reference,
            detected_product=line.detected_product,
            quantity=line.quantity,
            unit=line.unit,
            extraction_confidence=line.extraction_confidence,
            line_score=line.line_score,
            validation_status=line.validation_status,
            doubt_reason=line.doubt_reason,
        )
        db.add(new_line)
        db.flush()
        order.score = ScoringService().score_order(db, order)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.line.duplicate", entity_type="order_line", entity_id=new_line.id, message="Linea duplicada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/lines/{line_id}/delete")
def delete_order_line_post(order_id: int, line_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return delete_order_line(order_id, line_id, db, user)


@router.delete("/{order_id}/lines/{line_id}")
def delete_order_line(order_id: int, line_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    line = db.get(OrderLine, line_id)
    order = db.get(Order, order_id)
    if line and order and line.company_id == user.company_id and order.company_id == user.company_id:
        old_product = line.validated_product or line.product
        if old_product:
            LearningService().penalize_customer_product_knowledge(
                db,
                company_id=user.company_id,
                customer_id=order.validated_customer_id or order.customer_id,
                product_id=old_product.id,
                reason="Una linea sugerida fue eliminada manualmente y no debe contarse como aprendizaje aprobado.",
                order_id=order.id,
            )
        db.delete(line)
        db.flush()
        order.score = ScoringService().score_order(db, order)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.line.delete", entity_type="order_line", entity_id=line_id, message="Linea eliminada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/recalculate-score")
def recalculate_score(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        order.score = ScoringService().score_order(db, order)
        order.status = ScoringService().status_for_score(db, user.company_id, order.score)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.recalculate_score", entity_type="order", entity_id=order.id, message=f"Scoring recalculado: {order.score}")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/confirm")
def confirm_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.scalar(select(Order).where(Order.id == order_id, Order.company_id == user.company_id).options(selectinload(Order.lines)))
    if order and order.company_id == user.company_id:
        errors = validate_confirmation(order, get_or_create_settings(db, ScoringSettings, user.company_id))
        if errors:
            log_action(db, company_id=user.company_id, user=user, action="order.confirm.blocked", entity_type="order", entity_id=order.id, message=" | ".join(errors))
            return RedirectResponse("/orders", status_code=303)
        order.status = "pedido_confirmado"
        order.confirmed_at = datetime.now(timezone.utc)
        for line in order.lines or []:
            _sync_customer_product_knowledge(
                db,
                company_id=user.company_id,
                order=order,
                line=line,
                user=user,
                source_context="pedido_confirmado",
                force_habitual=False,
            )
        LearningService().record_case(
            db,
            company_id=user.company_id,
            summary=f"{_customer_label(order)} confirmado con {len(order.lines or [])} lineas.",
            resolved_action="pedido_confirmado",
            resolution_json=json.dumps({"order_id": order.id, "customer": _customer_label(order), "lines": len(order.lines or [])}, ensure_ascii=False),
            customer_id=order.validated_customer_id or order.customer_id,
            order_id=order.id,
        )
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.confirm", entity_type="order", entity_id=order.id, message="Pedido confirmado")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/force-confirm")
def force_confirm_order(order_id: int, force_reason: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id and user.role.name in {"Administrador", "Supervisor"} and force_reason.strip():
        order.status = "pedido_confirmado"
        order.confirmed_at = utcnow()
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.force_confirm", entity_type="order", entity_id=order.id, message=f"Confirmacion forzada: {force_reason}")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/export")
def export_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order:
        if order.company_id == user.company_id:
            job = enqueue_job(db, company_id=user.company_id, job_type="export_order", payload={"order_id": order.id}, created_by_user_id=user.id)
            log_action(db, company_id=user.company_id, user=user, action="order.export", entity_type="job", entity_id=job.id, message="Exportacion encolada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/generate-export")
def generate_export(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return export_order(order_id, db, user)


@router.get("/{order_id}/export-preview")
def export_preview(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    export = db.scalar(select(ExportFile).where(ExportFile.order_id == order_id, ExportFile.company_id == user.company_id).order_by(ExportFile.created_at.desc()))
    if not export:
        job = enqueue_job(db, company_id=user.company_id, job_type="export_order", payload={"order_id": order_id}, created_by_user_id=user.id)
        log_action(db, company_id=user.company_id, user=user, action="order.export_preview_queued", entity_type="job", entity_id=job.id, message="Vista previa de exportacion encolada")
        return RedirectResponse(f"/jobs/{job.id}/detail", status_code=303)
    return PlainTextResponse(export.content, media_type="text/plain")


@router.post("/{order_id}/export-ftp")
def export_ftp(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        job = enqueue_job(db, company_id=user.company_id, job_type="export_order_ftp", payload={"order_id": order.id}, created_by_user_id=user.id)
        log_action(db, company_id=user.company_id, user=user, action="order.export_ftp", entity_type="job", entity_id=job.id, message="Exportacion FTP encolada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/mark-not-order")
def mark_not_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        order.status = "no_pedido"
        if order.email:
            order.email.detected_type = "no_pedido"
            order.email.status = "no_pedido"
        for line in order.lines or []:
            if line.validated_product or line.product:
                LearningService().record_knowledge_conflict(
                    db,
                    company_id=user.company_id,
                    customer_id=order.validated_customer_id or order.customer_id,
                    product_id=(line.validated_product or line.product).id,
                    title="Pedido marcado como no pedido",
                    message="La línea detectada no debe incorporarse como aprendizaje aprobado.",
                    payload={"order_id": order.id, "line_id": line.id},
                    order_id=order.id,
                )
        LearningService().record_case(
            db,
            company_id=user.company_id,
            summary=f"{_customer_label(order)} marcado como no pedido.",
            resolved_action="no_pedido",
            resolution_json=json.dumps({"order_id": order.id, "customer": _customer_label(order)}, ensure_ascii=False),
            customer_id=order.validated_customer_id or order.customer_id,
            order_id=order.id,
        )
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.mark_not_order", entity_type="order", entity_id=order.id, message="Marcado como no pedido")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/discard")
def discard_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        order.status = "descartado"
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.discard", entity_type="order", entity_id=order.id, message="Pedido descartado")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/delete")
def delete_order_post(order_id: int, request: Request, reason: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return delete_order(order_id, request, reason, db, user)


@router.delete("/{order_id}")
def delete_order(order_id: int, request: Request, reason: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_delete(user):
        raise HTTPException(status_code=403, detail="Sin permisos para eliminar.")
    order = db.get(Order, order_id)
    if not order or order.company_id != user.company_id or order.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Pedido no encontrado.")
    _soft_delete_order(db, order, user, reason.strip() or None)
    return RedirectResponse(request.headers.get("referer") or "/orders", status_code=303)


@router.post("/bulk-delete")
def bulk_delete_orders(ids: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_delete(user):
        raise HTTPException(status_code=403, detail="Sin permisos para eliminar.")
    raw_ids = [int(item) for item in ids.split(",") if item.strip().isdigit()]
    deleted = 0
    for order in db.scalars(select(Order).where(Order.company_id == user.company_id, Order.id.in_(raw_ids), Order.deleted_at.is_(None))).all():
        _soft_delete_order(db, order, user)
        deleted += 1
    return JSONResponse({"success": True, "deleted": deleted, "message": "Pedidos eliminados correctamente"})


@router.post("/{order_id}/save-product-alias")
def save_product_alias(order_id: int, product_id: int = Form(...), alias: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    product = db.get(Product, product_id)
    if product and product.company_id == user.company_id and alias.strip():
        db.add(ProductAlias(company_id=user.company_id, product_id=product.id, alias=alias.strip()))
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="product.alias.save", entity_type="product", entity_id=product.id, message="Alias de producto guardado")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/save-customer-alias")
def save_customer_alias(order_id: int, customer_id: int = Form(...), alias: str = Form(""), domain: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    customer = db.get(Customer, customer_id)
    if customer and customer.company_id == user.company_id:
        if alias.strip():
            db.add(CustomerAlias(company_id=user.company_id, customer_id=customer.id, alias=alias.strip()))
            db.add(CustomerContactPoint(company_id=user.company_id, customer_id=customer.id, type="alias", value=alias.strip().lower(), label="alias", source="correction", active=True))
        if domain.strip():
            db.add(CustomerDomain(company_id=user.company_id, customer_id=customer.id, domain=domain.strip().lower()))
            db.add(CustomerContactPoint(company_id=user.company_id, customer_id=customer.id, type="domain", value=domain.strip().lower(), label="dominio", source="correction", active=True))
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="customer.alias.save", entity_type="customer", entity_id=customer.id, message="Alias/dominio de cliente guardado")
    return RedirectResponse("/orders", status_code=303)


@router.get("/{order_id}/exports/{export_id}/download")
def download_export(order_id: int, export_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    export = db.get(ExportFile, export_id)
    if not export or export.company_id != user.company_id or export.order_id != order_id:
        return PlainTextResponse("No encontrado", status_code=404)
    return PlainTextResponse(export.content, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{export.filename}"'})


@router.get("/{order_id}/attachments/{attachment_id}")
def view_attachment(order_id: int, attachment_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    attachment = db.get(EmailAttachment, attachment_id)
    order = db.get(Order, order_id)
    if not attachment or not order or attachment.company_id != user.company_id or order.company_id != user.company_id or order.email_id != attachment.email_id:
        return PlainTextResponse("No encontrado", status_code=404)
    path = Path(attachment.storage_path or "")
    if not path.exists() or not path.is_file():
        return PlainTextResponse("Archivo no disponible", status_code=404)
    media_type = attachment.content_type or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=attachment.filename)


@router.get("/{order_id}/attachments/{attachment_id}/preview")
def preview_attachment(order_id: int, attachment_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    attachment = db.get(EmailAttachment, attachment_id)
    order = db.get(Order, order_id)
    if not attachment or not order or attachment.company_id != user.company_id or order.company_id != user.company_id or order.email_id != attachment.email_id:
        return PlainTextResponse("No encontrado", status_code=404)
    path = Path(attachment.storage_path or "")
    if not path.exists() or not path.is_file():
        return PlainTextResponse("Archivo no disponible", status_code=404)
    media_type = "application/pdf" if attachment.is_pdf else attachment.content_type or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Content-Disposition": f'inline; filename="{attachment.filename}"'})
