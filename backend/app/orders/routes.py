import json
from datetime import datetime, timezone
from email.utils import parseaddr

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.agent.extraction.diagnostics import extraction_diagnostics_from_messages
from app.core.templating import templates
from app.agent.platform import LearningService
from app.agent.services import MockAgentService, ScoringService
from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.pagination import paginate
from app.db.models import Conversation, Customer, CustomerContactPoint, Email, EmailAttachment, ExportFile, FTPSettings, InboundMessage, ManualCorrection, Order, OrderLine, Product, RagCase, ScoringSettings, User, utcnow
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.orders.service import (
    _can_delete,
    _customer_label,
    _fmt_dt,
    _product_suggestions_for_line,
    _review_customer_snapshot,
    _review_product_candidates,
    _soft_delete_order,
    _sync_customer_product_knowledge,
    order_alerts,
    order_score_category,
    validate_confirmation,
)
from app.orders.state import ORDER_STATE
from app.settings.service import get_or_create_settings, resolve_updated_by_id
from app.tenancy.database import get_tenant_db
from app.core.attachment_storage import read_attachment

router = APIRouter(prefix="/orders", tags=["orders"])


def _learn_customer_email_from_confirmed_order(
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


def _conversation_preview(order: Order | None) -> dict | None:
    if not order or not order.conversation:
        return None
    ordered_messages = sorted(order.conversation.messages or [], key=lambda item: item.received_at or item.created_at)
    messages: list[dict[str, object]] = []
    use_transcript_payload = len(ordered_messages) <= 1
    for message in ordered_messages:
        parsed_payload = {}
        if use_transcript_payload and message.raw_payload_json:
            try:
                parsed_payload = json.loads(message.raw_payload_json)
            except json.JSONDecodeError:
                parsed_payload = {}
        parsed_messages = []
        if use_transcript_payload and parsed_payload.get("import_type") == "manual_whatsapp":
            parsed_messages = (parsed_payload.get("parsed") or {}).get("messages", []) if isinstance(parsed_payload.get("parsed"), dict) else []
        if parsed_messages:
            for parsed_message in parsed_messages:
                messages.append(
                    {
                        "sender": parsed_message.get("sender") or message.sender or "Cliente",
                        "direction": parsed_message.get("direction") or "inbound",
                        "role_label": "Empresa" if (parsed_message.get("direction") or "inbound") == "outbound" else "Cliente",
                        "text": parsed_message.get("text") or "",
                        "timestamp_label": parsed_message.get("timestamp_label") or "",
                    }
                )
        else:
            messages.append(
                {
                    "sender": message.sender or ("Empresa" if message.direction == "outbound" else "Cliente"),
                    "direction": message.direction or "inbound",
                    "role_label": "Empresa" if (message.direction or "inbound") == "outbound" else "Cliente",
                    "text": message.original_content or message.normalized_text or "",
                    "timestamp_label": message.received_at.strftime("%d/%m/%Y %H:%M") if message.received_at else "",
                }
            )
    if not messages:
        return None
    source_message = ordered_messages[0]
    provider = (source_message.provider or "").strip().lower()
    if provider == "manual_import":
        provider_label = "Importación manual"
    elif provider == "whatsapp":
        provider_label = "WhatsApp"
    elif provider:
        provider_label = provider.title()
    else:
        provider_label = "Conversación"
    return {
        "provider_label": provider_label,
        "messages": messages,
        "conversation": order.conversation,
    }


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
            stmt = stmt.where(or_(Order.status.in_(("pedido_pendiente_revision", "pending_review", "dudoso", "no_importable")), Order.score < scoring.safe_threshold))
        else:
            stmt = stmt.where(Order.status.in_(("pedido_confirmado", "pedido_exportado")))
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
            scoring,
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


@router.get("/customer-search")
def customer_search(
    q: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    query = q.strip()
    if len(query) < 2:
        return []

    exact = query
    starts = f"{query}%"
    contains = f"%{query}%"

    match = or_(
        Customer.code.ilike(contains),
        Customer.fiscal_name.ilike(contains),
        Customer.commercial_name.ilike(contains),
        Customer.tax_id.ilike(contains),
        Customer.primary_email.ilike(contains),
    )

    relevance = case(
        (
            or_(
                Customer.code.ilike(exact),
                Customer.fiscal_name.ilike(exact),
                Customer.commercial_name.ilike(exact),
                Customer.tax_id.ilike(exact),
                Customer.primary_email.ilike(exact),
            ),
            0,
        ),
        (
            or_(
                Customer.code.ilike(starts),
                Customer.fiscal_name.ilike(starts),
                Customer.commercial_name.ilike(starts),
                Customer.tax_id.ilike(starts),
                Customer.primary_email.ilike(starts),
            ),
            1,
        ),
        else_=2,
    )

    customers = db.scalars(
        select(Customer)
        .where(
            Customer.company_id == user.company_id,
            Customer.deleted_at.is_(None),
            match,
        )
        .order_by(relevance, Customer.fiscal_name.asc())
        .limit(15)
    ).all()

    return [
        {
            "id": customer.id,
            "code": customer.code or "",
            "name": customer.fiscal_name or customer.commercial_name or "",
            "tax_id": customer.tax_id or "",
            "email": customer.primary_email or "",
        }
        for customer in customers
    ]


@router.get("/product-search")
def product_search(
    q: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    query = q.strip()
    if len(query) < 2:
        return []

    exact = query
    starts = f"{query}%"
    contains = f"%{query}%"

    match = or_(
        Product.reference.ilike(contains),
        Product.alternative_code.ilike(contains),
        Product.name.ilike(contains),
        Product.description.ilike(contains),
    )

    relevance = case(
        (
            or_(
                Product.reference.ilike(exact),
                Product.alternative_code.ilike(exact),
                Product.name.ilike(exact),
            ),
            0,
        ),
        (
            or_(
                Product.reference.ilike(starts),
                Product.alternative_code.ilike(starts),
                Product.name.ilike(starts),
            ),
            1,
        ),
        else_=2,
    )

    products = db.scalars(
        select(Product)
        .where(
            Product.company_id == user.company_id,
            Product.deleted_at.is_(None),
            match,
        )
        .order_by(relevance, Product.name.asc())
        .limit(15)
    ).all()

    return [
        {
            "id": product.id,
            "reference": product.reference or "",
            "name": product.name or product.description or "",
            "sale_price": product.sale_price or 0,
        }
        for product in products
    ]


@router.get("/{order_id}")
def order_detail(
    order_id: int,
    request: Request,
    product_q: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.company_id == user.company_id)
        .options(
            load_only(
                Order.id,
                Order.email_id,
                Order.customer_id,
                Order.validated_customer_id,
                Order.customer_detected_name,
                Order.customer_score,
                Order.order_date,
                Order.requested_delivery_date,
                Order.notes,
                Order.score,
                Order.status,
                Order.review_reasons,
                Order.created_at,
                Order.confirmed_at,
                Order.exported_at,
            ),
            selectinload(Order.lines)
            .load_only(
                OrderLine.id,
                OrderLine.company_id,
                OrderLine.order_id,
                OrderLine.product_id,
                OrderLine.validated_product_id,
                OrderLine.original_text,
                OrderLine.detected_reference,
                OrderLine.detected_product,
                OrderLine.quantity,
                OrderLine.unit,
                OrderLine.extraction_confidence,
                OrderLine.line_score,
                OrderLine.validation_status,
                OrderLine.doubt_reason,
            ),
            selectinload(Order.lines)
            .joinedload(OrderLine.product)
            .load_only(
                Product.id,
                Product.reference,
                Product.name,
                Product.sale_price,
            ),
            selectinload(Order.lines)
            .joinedload(OrderLine.validated_product)
            .load_only(
                Product.id,
                Product.reference,
                Product.name,
                Product.sale_price,
            ),
            selectinload(Order.email)
            .load_only(
                Email.id,
                Email.sender,
                Email.subject,
                Email.body,
                Email.detected_type,
                Email.received_at,
                Email.external_id,
                Email.extracted_text,
            )
            .selectinload(Email.attachments)
            .load_only(
                EmailAttachment.id,
                EmailAttachment.filename,
                EmailAttachment.content_type,
                EmailAttachment.size_bytes,
                EmailAttachment.extracted_text,
                EmailAttachment.extraction_error,
                EmailAttachment.is_pdf,
            ),
            selectinload(Order.conversation)
            .load_only(
                Conversation.id,
                Conversation.company_id,
                Conversation.channel_id,
                Conversation.provider,
                Conversation.external_thread_id,
                Conversation.status,
                Conversation.subject,
                Conversation.last_activity_at,
            )
            .selectinload(Conversation.messages)
            .load_only(
                InboundMessage.id,
                InboundMessage.company_id,
                InboundMessage.conversation_id,
                InboundMessage.provider,
                InboundMessage.sender,
                InboundMessage.recipient,
                InboundMessage.original_content,
                InboundMessage.normalized_text,
                InboundMessage.extraction_json,
                InboundMessage.raw_payload_json,
                InboundMessage.direction,
                InboundMessage.received_at,
                InboundMessage.status,
            ),
        )
    )
    products = _review_product_candidates(db, company_id=user.company_id, order=order, query=product_q) if order else []
    customer_source = None
    if order:
        customer_source = db.scalar(
            select(Customer)
            .where(
                Customer.company_id == user.company_id,
                Customer.deleted_at.is_(None),
                Customer.id == (order.validated_customer_id or order.customer_id),
            )
            .options(
                load_only(
                    Customer.id,
                    Customer.code,
                    Customer.fiscal_name,
                    Customer.commercial_name,
                    Customer.primary_email,
                    Customer.phone,
                    Customer.delegation,
                    Customer.status,
                )
            )
        )
    customer_context = _review_customer_snapshot(db, order, customer_source) if order else {"identified": False}
    line_suggestions = {line.id: _product_suggestions_for_line(products, line) for line in (order.lines or [])}
    conversation_preview = _conversation_preview(order) if order and not order.email_id else None
    extraction_diagnostics = extraction_diagnostics_from_messages(order.conversation.messages if order and order.conversation else [])
    return templates.TemplateResponse(
        "orders/detail.html",
        {
            "request": request,
            "user": user,
            "order": order,
            "products": products,
            "customer_context": customer_context,
            "line_suggestions": line_suggestions,
            "product_q": product_q,
            "conversation_preview": conversation_preview,
            "extraction_diagnostics": extraction_diagnostics,
        },
    )


@router.post("/{order_id}/customer")
def update_order_customer(order_id: int, validated_customer_id: int = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    new_customer = db.get(Customer, validated_customer_id)
    if (
        order
        and order.company_id == user.company_id
        and new_customer
        and new_customer.company_id == user.company_id
    ):
        previous_customer = order.validated_customer or order.customer
        order.validated_customer_id = new_customer.id
        order.customer_id = new_customer.id
        if not previous_customer or previous_customer.id != new_customer.id:
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
                    created_by_user_id=resolve_updated_by_id(db, user),
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
        log_action(db, company_id=user.company_id, user=user, action="order.customer.update", entity_type="order", entity_id=order.id, message="Cliente validado actualizado")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/save")
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
    new_customer = db.get(Customer, validated_customer_id) if validated_customer_id else None
    customer_is_valid = (
        not validated_customer_id
        or (new_customer is not None and new_customer.company_id == user.company_id)
    )
    if order and order.company_id == user.company_id and customer_is_valid:
        previous_notes = order.notes or ""
        previous_customer_id = order.validated_customer_id or order.customer_id
        order.validated_customer_id = new_customer.id if new_customer else None
        order.customer_id = new_customer.id if new_customer else order.customer_id

        normalized_order_date = order_date.strip()
        if normalized_order_date:
            try:
                normalized_order_date = datetime.strptime(
                    normalized_order_date,
                    "%d-%m-%Y",
                ).strftime("%Y-%m-%d")
            except ValueError:
                pass

        order.order_date = normalized_order_date
        order.requested_delivery_date = requested_delivery_date
        order.notes = notes
        if status:
            ORDER_STATE.change_state(order, status)
        if new_customer and new_customer.id != previous_customer_id:
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
                        created_by_user_id=resolve_updated_by_id(db, user),
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
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


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
    new_product = db.get(Product, validated_product_id) if validated_product_id else None
    product_is_valid = (
        not validated_product_id
        or (new_product is not None and new_product.company_id == user.company_id)
    )
    if (
        line
        and order
        and line.company_id == user.company_id
        and order.company_id == user.company_id
        and line.order_id == order.id
        and product_is_valid
    ):
        old_product = line.validated_product or line.product
        old_quantity = line.quantity
        old_unit = line.unit
        line.validated_product_id = new_product.id if new_product else None
        line.product_id = new_product.id if new_product else line.product_id
        line.quantity = quantity
        line.unit = unit
        line.validation_status = "validated" if line.validated_product_id and quantity else "pending"
        line.doubt_reason = "" if line.validation_status == "validated" else "Linea pendiente de validar"
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
                    created_by_user_id=resolve_updated_by_id(db, user),
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
                    created_by_user_id=resolve_updated_by_id(db, user),
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
                    created_by_user_id=resolve_updated_by_id(db, user),
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
        ORDER_STATE.apply_score(db, order, user.company_id, order.score)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.line.update", entity_type="order_line", entity_id=line.id, message="Linea de pedido actualizada")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


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
    product_is_valid = (
        not validated_product_id
        or (product is not None and product.company_id == user.company_id)
    )
    if order and order.company_id == user.company_id and product_is_valid:
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
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/lines/{line_id}/duplicate")
def duplicate_order_line(order_id: int, line_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    line = db.get(OrderLine, line_id)
    order = db.get(Order, order_id)
    if (
        line
        and order
        and line.company_id == user.company_id
        and order.company_id == user.company_id
        and line.order_id == order.id
    ):
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
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/lines/{line_id}/delete")
def delete_order_line_post(order_id: int, line_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return delete_order_line(order_id, line_id, db, user)


@router.delete("/{order_id}/lines/{line_id}")
def delete_order_line(order_id: int, line_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    line = db.get(OrderLine, line_id)
    order = db.get(Order, order_id)
    if (
        line
        and order
        and line.company_id == user.company_id
        and order.company_id == user.company_id
        and line.order_id == order.id
    ):
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
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/recalculate-score")
def recalculate_score(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        order.score = ScoringService().score_order(db, order)
        ORDER_STATE.apply_score(db, order, user.company_id, order.score)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.recalculate_score", entity_type="order", entity_id=order.id, message=f"Scoring recalculado: {order.score}")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/reprocess")
def reprocess_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        job = enqueue_job(db, company_id=user.company_id, job_type="process_order", payload={"order_id": order.id}, created_by_user_id=resolve_updated_by_id(db, user))
        log_action(db, company_id=user.company_id, user=user, action="order.reprocess", entity_type="job", entity_id=job.id, message="Reprocesamiento de pedido encolado")
        db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/validate")
@router.post("/{order_id}/confirm")
def confirm_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.scalar(select(Order).where(Order.id == order_id, Order.company_id == user.company_id).options(selectinload(Order.lines)))
    if order and order.company_id == user.company_id:
        errors = validate_confirmation(order, get_or_create_settings(db, ScoringSettings, user.company_id))
        if errors:
            log_action(db, company_id=user.company_id, user=user, action="order.confirm.blocked", entity_type="order", entity_id=order.id, message=" | ".join(errors))
            return RedirectResponse(f"/orders/{order_id}", status_code=303)
        ORDER_STATE.confirm(order, when=datetime.now(timezone.utc))
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

        email_learning_result = _learn_customer_email_from_confirmed_order(
            db,
            order=order,
            company_id=user.company_id,
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
        if email_learning_result == "conflict":
            log_action(
                db,
                company_id=user.company_id,
                user=user,
                action="customer.email_learning.conflict",
                entity_type="order",
                entity_id=order.id,
                message="El email remitente ya estaba asociado a otro cliente y no se ha sobrescrito.",
            )
        log_action(db, company_id=user.company_id, user=user, action="order.confirm", entity_type="order", entity_id=order.id, message="Pedido confirmado")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/{order_id}/force-confirm")
def force_confirm_order(order_id: int, force_reason: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id and user.role.name in {"Administrador", "Supervisor"} and force_reason.strip():
        ORDER_STATE.confirm(order, when=utcnow())
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="order.force_confirm", entity_type="order", entity_id=order.id, message=f"Confirmacion forzada: {force_reason}")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/export")
def export_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order:
        if order.company_id == user.company_id:
            job = enqueue_job(db, company_id=user.company_id, job_type="export_order", payload={"order_id": order.id}, created_by_user_id=resolve_updated_by_id(db, user))
            log_action(db, company_id=user.company_id, user=user, action="order.export", entity_type="job", entity_id=job.id, message="Exportacion encolada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/generate-export")
def generate_export(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return export_order(order_id, db, user)


@router.get("/{order_id}/export-preview")
def export_preview(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    export = db.scalar(select(ExportFile).where(ExportFile.order_id == order_id, ExportFile.company_id == user.company_id).order_by(ExportFile.created_at.desc()))
    if not export:
        job = enqueue_job(db, company_id=user.company_id, job_type="export_order", payload={"order_id": order_id}, created_by_user_id=resolve_updated_by_id(db, user))
        log_action(db, company_id=user.company_id, user=user, action="order.export_preview_queued", entity_type="job", entity_id=job.id, message="Vista previa de exportacion encolada")
        return RedirectResponse(f"/jobs/{job.id}/detail", status_code=303)
    return PlainTextResponse(export.content, media_type="text/plain")


@router.post("/{order_id}/export-ftp")
def export_ftp(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        job = enqueue_job(db, company_id=user.company_id, job_type="export_order_ftp", payload={"order_id": order.id}, created_by_user_id=resolve_updated_by_id(db, user))
        log_action(db, company_id=user.company_id, user=user, action="order.export_ftp", entity_type="job", entity_id=job.id, message="Exportacion FTP encolada")
    return RedirectResponse("/orders", status_code=303)


@router.post("/{order_id}/mark-not-order")
def mark_not_order(order_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    order = db.get(Order, order_id)
    if order and order.company_id == user.company_id:
        ORDER_STATE.mark_no_order(order)
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
        ORDER_STATE.discard(order)
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

    try:
        content = read_attachment(attachment.storage_path or "")
    except Exception:
        return PlainTextResponse("Archivo no disponible", status_code=404)

    media_type = attachment.content_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"'
        },
    )


@router.get("/{order_id}/attachments/{attachment_id}/preview")
def preview_attachment(order_id: int, attachment_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    attachment = db.get(EmailAttachment, attachment_id)
    order = db.get(Order, order_id)
    if not attachment or not order or attachment.company_id != user.company_id or order.company_id != user.company_id or order.email_id != attachment.email_id:
        return PlainTextResponse("No encontrado", status_code=404)

    try:
        content = read_attachment(attachment.storage_path or "")
    except Exception:
        return PlainTextResponse("Archivo no disponible", status_code=404)

    media_type = "application/pdf" if attachment.is_pdf else attachment.content_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{attachment.filename}"'
        },
    )
