from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core.entry_workflow import canonical_email_status, canonical_inbound_status, entry_status_label
from app.core.channel_identity import channel_label, order_channel_key
from app.core.timezones import format_local_datetime
from app.core.pagination import normalize_page
from app.db.models import Company, Conversation, Customer, CustomerContactPoint, CustomerDomain, Email, FTPSettings, InboundMessage, LLMSettings, Order, OrderLine, ScoringSettings
from app.orders.state import CONFIRMED_ORDER_STATUSES, ERROR_ORDER_STATUSES, ORDER_STATE, PENDING_ORDER_STATUSES
from app.orders.scoring import is_positive_quantity
from app.settings.service import get_or_create_settings


def _safe_sort_timestamp(value) -> float:  # noqa: ANN001
    if isinstance(value, datetime):
        aware_value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware_value.timestamp()
    return 0.0


def scoring_category(score: float | None, settings: ScoringSettings) -> str:
    return ORDER_STATE.scoring_category(score, settings)


def category_label(category: str) -> str:
    return {
        "safe": "Confianza alta",
        "reviewable": "Revisable",
        "doubtful": "Dudosa",
        "not_importable": "Bloqueada",
        "without_score": "Pendiente de analizar",
    }.get(category, category)


def order_operational_category(order: Order, settings: ScoringSettings, *, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> str:
    return ORDER_STATE.operational_state(order, settings, line_metrics=line_metrics_by_order)


def _order_line_metrics(order: Order, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> dict[str, int]:
    if line_metrics_by_order and order.id is not None and order.id in line_metrics_by_order:
        return line_metrics_by_order[order.id]
    lines = order.lines or []
    return {
        "line_count": len(lines),
        "doubt_count": sum(1 for line in lines if line.validation_status != "validated" or not line.validated_product_id or line.doubt_reason),
        "missing_product_count": sum(1 for line in lines if not line.validated_product_id),
        "invalid_quantity_count": sum(1 for line in lines if not is_positive_quantity(line.quantity)),
    }


def _load_order_line_metrics(db: Session, company_id: int, order_ids: list[int]) -> dict[int, dict[str, int]]:
    if not order_ids:
        return {}
    rows = db.execute(
        select(
            OrderLine.order_id,
            func.count(OrderLine.id).label("line_count"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (OrderLine.validation_status != "validated")
                            | (OrderLine.validated_product_id.is_(None))
                            | (OrderLine.doubt_reason.is_not(None)),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("doubt_count"),
            func.coalesce(func.sum(case((OrderLine.validated_product_id.is_(None), 1), else_=0)), 0).label("missing_product_count"),
            func.coalesce(func.sum(case(((OrderLine.quantity.is_(None)) | (OrderLine.quantity <= 0), 1), else_=0)), 0).label("invalid_quantity_count"),
        )
        .where(OrderLine.company_id == company_id, OrderLine.order_id.in_(order_ids))
        .group_by(OrderLine.order_id)
    ).all()
    return {
        int(row.order_id): {
            "line_count": int(row.line_count or 0),
            "doubt_count": int(row.doubt_count or 0),
            "missing_product_count": int(row.missing_product_count or 0),
            "invalid_quantity_count": int(row.invalid_quantity_count or 0),
        }
        for row in rows
    }


def validate_blockers(order: Order, settings: ScoringSettings, *, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> list[str]:
    return ORDER_STATE.validate_blockers(order, settings, line_metrics=line_metrics_by_order)


def order_origin(order: Order) -> str:
    if order_channel_key(order) == "whatsapp":
        return "WhatsApp"
    has_pdf = bool(order.email and order.email.has_pdf)
    has_body = bool(order.email and order.email.body)
    has_attachments = bool(order.email and order.email.has_attachments)
    if has_pdf and has_body:
        return "PDF + Email"
    if has_pdf:
        return "PDF"
    if has_attachments:
        return "Adjunto"
    if has_body:
        return "Email"
    return "Sin documento"


def order_has_pdf(order: Order) -> bool:
    if order.email:
        return bool(order.email.has_pdf or any(attachment.is_pdf for attachment in order.email.attachments))
    if order.conversation:
        return any(
            attachment.content_type == "application/pdf" or (attachment.filename or "").lower().endswith(".pdf")
            for message in order.conversation.messages or []
            for attachment in message.attachments or []
        )
    return False


def _latest_inbound_message(order: Order):  # noqa: ANN001
    if not order.conversation:
        return None
    inbound_messages = [
        message
        for message in (order.conversation.messages or [])
        if message.direction == "inbound" and (message.sender or "").strip()
    ]
    return max(
        inbound_messages,
        key=lambda message: _safe_sort_timestamp(message.received_at or message.created_at),
        default=None,
    )


def order_subject(order: Order) -> str:
    if order.email:
        return order.email.subject or ""
    if order.conversation and order.conversation.subject:
        return order.conversation.subject
    return "Conversación WhatsApp" if order_channel_key(order) == "whatsapp" else ""


def order_sender(order: Order) -> str:
    if order.email:
        return order.email.sender or ""
    if not order.conversation:
        return ""
    latest_inbound = _latest_inbound_message(order)
    if latest_inbound:
        return latest_inbound.sender.strip()
    return order.conversation.external_thread_id or ""


def _load_order_conversations(db: Session, orders: list[Order], company_id: int) -> None:
    """Load conversation metadata only for conversation-backed orders.

    Most orders are email-backed and must keep the home/workbench query budget
    small. WhatsApp orders have no email, so they are loaded in one batched
    query only when they are actually present.
    """
    conversation_ids = {
        order.conversation_id
        for order in orders
        if order.conversation_id and not order.email_id
    }
    if not conversation_ids:
        return
    conversations = db.scalars(
        select(Conversation)
        .where(
            Conversation.company_id == company_id,
            Conversation.id.in_(conversation_ids),
        )
        .options(selectinload(Conversation.messages).selectinload(InboundMessage.attachments))
    ).unique().all()
    by_id = {conversation.id: conversation for conversation in conversations}
    for order in orders:
        conversation = by_id.get(order.conversation_id)
        if conversation:
            set_committed_value(order, "conversation", conversation)


def order_issue_summary(order: Order, settings: ScoringSettings, *, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> str:
    blockers = validate_blockers(order, settings, line_metrics_by_order=line_metrics_by_order)
    if blockers:
        return " · ".join(blockers)
    metrics = _order_line_metrics(order, line_metrics_by_order)
    if metrics["missing_product_count"]:
        return f"{metrics['missing_product_count']} productos no encontrados"
    if metrics["invalid_quantity_count"]:
        return f"{metrics['invalid_quantity_count']} cantidades dudosas"
    if order.customer_identification_method:
        return f"Cliente por {order.customer_identification_method}"
    return "Sin incidencias relevantes"


def recent_processed_emails_overview(db: Session, company_id: int, *, days: int = 7, limit: int = 8) -> list[dict]:
    company = db.get(Company, company_id)
    timezone_name = company.timezone if company else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    emails = db.scalars(
        select(Email)
        .where(
            Email.company_id == company_id,
            Email.received_at >= cutoff,
            Email.agent_status != "not_processed",
        )
        .order_by(Email.received_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": email.id,
            "sender": email.sender,
            "subject": email.subject,
            "received_at": email.received_at,
            "received_label": format_local_datetime(email.received_at, timezone_name, "%d/%m %H:%M", "--:--"),
            "agent_status": email.agent_status,
            "status_label": {
                "processed_order_detected": "Pedido detectado",
                "processed_no_order": "Sin pedido",
                "processed_doubtful": "Dudoso",
                "processed": "Procesado",
            }.get(email.agent_status, email.agent_status.replace("_", " ").title()),
            "detected_type": email.detected_type,
        }
        for email in emails
    ]


def order_priority(order: Order, settings: ScoringSettings, *, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> tuple[int, str]:
    op = order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order)
    if op == "error":
        return 1, "Error"
    if op == "blocked":
        return 2, "Bloqueado"
    if scoring_category(order.score, settings) == "doubtful":
        return 3, "Alta"
    if op == "review":
        return 4, "Media"
    if op == "ready":
        return 5, "Baja"
    return 6, "Baja"


def order_action(order: Order, settings: ScoringSettings, *, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> tuple[str, str]:
    op = order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order)
    if op == "error":
        return "Reintentar envio", "retry"
    if op == "blocked":
        return "Revisar", "resolve"
    if op == "ready":
        return "Enviar a gestión", "confirm"
    return "Revisar", "review"


def build_order_item(order: Order, settings: ScoringSettings, *, include_line_metrics: bool = True, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> dict:
    category = scoring_category(order.score, settings)
    metrics = _order_line_metrics(order, line_metrics_by_order)
    priority_rank, priority_label = order_priority(order, settings, line_metrics_by_order=line_metrics_by_order)
    action_label, action_type = order_action(order, settings, line_metrics_by_order=line_metrics_by_order)
    channel_key = order_channel_key(order)
    return {
        "id": order.id,
        "kind": "order",
        "priority_rank": priority_rank,
        "priority": priority_label,
        "date": order.created_at,
        "channel_key": channel_key,
        "channel": channel_label(channel_key),
        "customer": (order.validated_customer or order.customer).fiscal_name if (order.validated_customer or order.customer) else order.customer_detected_name or "Cliente no identificado",
        "subject": order_subject(order),
        "sender": order_sender(order),
        "origin": order_origin(order),
        "line_count": metrics["line_count"] if include_line_metrics else 0,
        "doubt_count": metrics["doubt_count"] if include_line_metrics else 0,
        "doubt_text": order_issue_summary(order, settings, line_metrics_by_order=line_metrics_by_order) if include_line_metrics else "",
        "score": order.score,
        "category": category,
        "category_label": category_label(category),
        "status": order.status,
        "action_label": action_label,
        "action_type": action_type,
        "has_pdf": bool(order.email and order.email.has_pdf),
        "detail_url": f"/workbench/item/order/{order.id}/detail",
    }


def email_has_pdf(email: Email) -> bool:
    return bool(email.has_pdf)


def email_origin(email: Email) -> str:
    has_pdf = bool(email.has_pdf)
    has_body = bool(email.body)
    has_attachments = bool(email.has_attachments)
    if has_pdf and has_body:
        return "PDF + Email"
    if has_pdf:
        return "PDF"
    if has_attachments:
        return "Adjunto"
    if has_body:
        return "Email"
    return "Sin adjunto"


def email_agent_status(email: Email, order: Order | None = None) -> str:
    status = canonical_email_status(email)
    if status == "review":
        return "doubtful"
    if status == "processed" and order:
        return "order_detected"
    if status in {"not_processed", "processing", "error", "no_order", "discarded", "closed", "archived", "queued"}:
        return status
    return "processed"


def agent_status_label(status: str) -> str:
    key = (status or "").strip().lower()
    if key == "review":
        return "Pendiente de validar"
    if key == "order_detected":
        return "Procesado como pedido"
    if key == "no_order":
        return "No es pedido"
    if key == "discarded":
        return "Descartado"
    if key == "processed":
        return "Procesado"
    if key == "processing":
        return "Procesando"
    if key in {"not_processed", "queued", "pending_reprocess"}:
        return "Pendiente"
    if key == "error":
        return "Error"
    return entry_status_label(status)



def operational_status_for_order(order: Order) -> tuple[str, str]:
    status = (order.status or "").strip().lower()

    if status in {
        "pedido_exportado",
        "cerrado",
        "cancelado",
        "descartado",
        "deleted",
        "archived_deleted",
    }:
        return "archived", "Archivado"

    if status in CONFIRMED_ORDER_STATUSES:
        return "confirmed", "Pedido confirmado"

    return "pending_validation", "Pendiente de validación"


def operational_status_for_email(email: Email) -> tuple[str, str]:
    status = canonical_email_status(email)

    if status in {"closed", "archived", "discarded", "no_order"}:
        return "archived", "Archivado"

    return "pending_classification", "Pendiente de clasificar"


def order_workbench_item(order: Order, settings: ScoringSettings, *, include_line_metrics: bool = True, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> dict:
    item = build_order_item(order, settings, include_line_metrics=include_line_metrics, line_metrics_by_order=line_metrics_by_order)
    agent_status = email_agent_status(order.email, order) if order.email else "order_detected"
    op_category = order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order)
    action_label, action_type = order_action(order, settings, line_metrics_by_order=line_metrics_by_order)
    channel_key = order_channel_key(order)
    operational_status, operational_status_label = operational_status_for_order(order)
    return {
        "id": f"order-{order.id}",
        "kind": "order",
        "channel_key": channel_key,
        "channel": channel_label(channel_key),
        "email_id": order.email_id,
        "order_id": order.id,
        # An order without a linked email is not an unread email.
        "is_read": True if order.email is None else bool(order.email.is_read),
        "is_favorite": bool(order.email and getattr(order.email, "is_favorite", False)),
        "received_at": order.email.received_at if order.email else order.created_at,
        "from_email": order_sender(order),
        "sender_domain": _safe_sender_domain(order_sender(order) if order.email else None),
        "subject": order_subject(order),
        "customer_name": item["customer"],
        "suggested_customer": item["customer"],
        "agent_status": agent_status,
        "agent_status_label": agent_status_label(agent_status),
        "operational_status": operational_status,
        "operational_status_label": operational_status_label,
        "detected_type": order.email.detected_type if order.email else "pedido",
        "has_pdf": item["has_pdf"],
        "has_attachments": bool(order.email and order.email.has_attachments),
        "origin": item["origin"],
        "score": item["score"],
        "scoring_category": "blocked" if op_category == "blocked" else item["category"],
        "scoring_label": "Bloqueada" if op_category == "blocked" else item["category_label"],
        "score_reason": item["doubt_text"],
        "doubts_summary": f"{item['doubt_count']} dudas" if item["doubt_count"] else "0 dudas",
        "order_status": order.status,
        "order_status_label": "Listo para confirmar" if op_category == "ready" else order.status,
        "available_actions": [action_type, "review", "reply"],
        "action_label": action_label,
        "priority_rank": item["priority_rank"],
        "line_count": item["line_count"],
        "detail_url": f"/workbench/item/order/{order.id}/detail",
        "modal_id": f"dashboard-order-modal-{order.id}",
    }


def email_workbench_item(email: Email) -> dict:
    agent_status = email_agent_status(email)
    operational_status, operational_status_label = operational_status_for_email(email)
    action = "Procesar ahora" if agent_status == "not_processed" else "Revisar"
    sender_domain = _safe_sender_domain(email.sender)
    priority_rank = 1 if agent_status == "error" else 3 if agent_status == "not_processed" else 7
    priority = "Alta" if priority_rank <= 2 else "Media" if priority_rank <= 4 else "Baja"
    return {
        "id": f"email-{email.id}",
        "kind": "email",
        "channel_key": "email",
        "email_id": email.id,
        "order_id": None,
        "is_read": bool(getattr(email, "is_read", False)),
        "is_favorite": bool(getattr(email, "is_favorite", False)),
        "received_at": email.received_at,
        "channel": "Email",
        "from_email": email.sender,
        "sender_domain": sender_domain,
        "subject": email.subject,
        "customer_name": "Cliente no identificado",
        "suggested_customer": "",
        "agent_status": agent_status,
        "agent_status_label": agent_status_label(agent_status),
        "operational_status": operational_status,
        "operational_status_label": operational_status_label,
        "detected_type": email.detected_type or "",
        "has_pdf": email_has_pdf(email),
        "has_attachments": bool(email.has_attachments),
        "origin": email_origin(email),
        "score": None,
        "scoring_category": "without_score",
        "scoring_label": "Pendiente de analizar",
        "score_reason": email.processing_error or ("Pendiente de analizar" if agent_status == "not_processed" else "Sin pedido asociado"),
        "doubts_summary": "Pendiente de analizar" if agent_status == "not_processed" else "0 dudas",
        "order_status": "",
        "order_status_label": "Pendiente" if agent_status == "not_processed" else "Sin pedido",
        "available_actions": ["process", "mark_no_order", "discard"] if agent_status == "not_processed" else ["reprocess", "close"],
        "action_label": action,
        "priority_rank": priority_rank,
        "priority": priority,
        "line_count": 0,
        "modal_id": f"email-modal-{email.id}",
        "detail_url": f"/workbench/item/email/{email.id}/detail",
    }


def _safe_sender_domain(sender: str | None) -> str:
    if not sender or "@" not in sender:
        return ""
    return sender.split("@", 1)[-1]


def _customer_suggestion_maps(db: Session, company_id: int) -> dict[str, dict[str, str]]:
    suggestion_rows = db.execute(
        union_all(
            select(
                literal("email").label("kind"),
                CustomerContactPoint.value.label("value"),
                Customer.fiscal_name.label("fiscal_name"),
            )
            .join(Customer, Customer.id == CustomerContactPoint.customer_id)
            .where(
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.active == True,  # noqa: E712
            ),
            select(
                literal("domain").label("kind"),
                CustomerContactPoint.value.label("value"),
                Customer.fiscal_name.label("fiscal_name"),
            )
            .join(Customer, Customer.id == CustomerContactPoint.customer_id)
            .where(
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type == "domain",
                CustomerContactPoint.active == True,  # noqa: E712
            ),
            select(
                literal("customer_domain").label("kind"),
                CustomerDomain.domain.label("value"),
                Customer.fiscal_name.label("fiscal_name"),
            )
            .join(Customer, Customer.id == CustomerDomain.customer_id)
            .where(CustomerDomain.company_id == company_id),
        )
    ).all()
    maps = {"email": {}, "domain": {}, "customer_domain": {}}
    for kind, value, fiscal_name in suggestion_rows:
        if value:
            maps[kind][str(value).strip().lower()] = fiscal_name
    return {
        "email": maps["email"],
        "domain": maps["domain"],
        "customer_domain": maps["customer_domain"],
    }


def suggest_customer_for_email(db: Session, company_id: int, email: Email, *, suggestion_maps: dict[str, dict[str, str]] | None = None) -> str:
    sender_value = (email.sender or "").strip().lower()
    if "@" not in sender_value:
        return ""
    sender = sender_value
    domain = sender.split("@", 1)[-1]
    lookup = suggestion_maps or _customer_suggestion_maps(db, company_id)
    if sender in lookup["email"]:
        return lookup["email"][sender]
    if domain in lookup["domain"]:
        return lookup["domain"][domain]
    if domain in lookup["customer_domain"]:
        return lookup["customer_domain"][domain]
    return ""


def filter_orders_for_operation(orders: list[Order], settings: ScoringSettings, filters: dict, line_metrics_by_order: dict[int, dict[str, int]] | None = None) -> list[Order]:
    work_status = filters.get("work_status")
    if work_status:
        orders = [order for order in orders if order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) == work_status]
    issue_type = filters.get("issue_type")
    if issue_type:
        if issue_type == "cliente_no_identificado":
            orders = [order for order in orders if not order.validated_customer_id]
        elif issue_type == "producto_no_encontrado":
            orders = [order for order in orders if _order_line_metrics(order, line_metrics_by_order)["missing_product_count"] > 0]
        elif issue_type == "cantidad_dudosa":
            orders = [order for order in orders if _order_line_metrics(order, line_metrics_by_order)["invalid_quantity_count"] > 0]
        elif issue_type == "error_exportacion":
            orders = [order for order in orders if order.status == "error_exportacion"]
        elif issue_type in {"error_correo", "error_llm", "error_ftp"}:
            orders = [order for order in orders if order.status.startswith("error")]
    origin = filters.get("origin")
    if origin:
        orders = [order for order in orders if origin.lower() in order_origin(order).lower()]
    has_pdf = filters.get("has_pdf")
    if has_pdf:
        want_pdf = has_pdf == "yes"
        orders = [order for order in orders if order_has_pdf(order) == want_pdf]
    requires_review = filters.get("requires_review")
    if requires_review:
        orders = [order for order in orders if (order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) in {"review", "blocked", "error"}) == (requires_review == "yes")]
    return orders


def _order_view_pagination(total_items: int, page: int, page_size: int) -> tuple[list[int], dict[str, int | bool | tuple[int, ...]]]:
    page, page_size = normalize_page(page, page_size)
    total_pages = ceil(total_items / page_size) if total_items else 0
    if total_pages and page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    end = min(start + page_size, total_items)
    return list(range(start, end)), {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
        "start_item": start + 1 if total_items else 0,
        "end_item": end,
        "allowed_page_sizes": (10, 25, 50, 100),
    }


def load_order_view_data(db: Session, company_id: int, filters: dict) -> dict:
    """Load the canonical order dataset used by cards and the table view.

    The inbox/workbench can also display standalone emails, but the two order
    views must never derive their rows from that mixed dataset. This helper is
    deliberately order-only and applies the shared filters before either
    renderer builds its own representation.
    """
    settings = get_or_create_settings(db, ScoringSettings, company_id)
    archived = filters.get("archived") in {True, "true", "1", "yes"}
    stmt = (
        select(Order)
        .where(
            Order.company_id == company_id,
            Order.deleted_at.is_(None),
            Order.archived.is_(archived),
        )
    )

    date_range = filters.get("date_range") or filters.get("quick_range")
    today = datetime.now(timezone.utc).date()
    if date_range == "today":
        stmt = stmt.where(Order.created_at >= datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))
    elif date_range == "yesterday":
        yesterday = today.fromordinal(today.toordinal() - 1)
        stmt = stmt.where(
            Order.created_at >= datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc),
            Order.created_at < datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
        )
    elif date_range == "7d":
        start = today.fromordinal(today.toordinal() - 6)
        stmt = stmt.where(Order.created_at >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc))
    if filters.get("date_from"):
        stmt = stmt.where(Order.created_at >= datetime.fromisoformat(filters["date_from"]).replace(tzinfo=timezone.utc))
    if filters.get("date_to"):
        stmt = stmt.where(Order.created_at <= datetime.fromisoformat(f"{filters['date_to']}T23:59:59").replace(tzinfo=timezone.utc))
    if filters.get("customer_id") and str(filters["customer_id"]) != "0":
        customer_id = int(filters["customer_id"])
        stmt = stmt.where((Order.customer_id == customer_id) | (Order.validated_customer_id == customer_id))
    if filters.get("score_min"):
        stmt = stmt.where(Order.score >= float(filters["score_min"]))
    if filters.get("score_max"):
        stmt = stmt.where(Order.score <= float(filters["score_max"]))
    if filters.get("status") or filters.get("order_status"):
        stmt = stmt.where(Order.status == (filters.get("status") or filters.get("order_status")))

    search = filters.get("search") or filters.get("customer_or_sender")
    needs_email_join = bool(
        filters.get("email_type")
        or filters.get("sender")
        or search
        or filters.get("sort") in {"sender_asc", "sender_desc", "subject_asc", "subject_desc", "type_asc", "type_desc"}
    )
    if needs_email_join:
        stmt = stmt.outerjoin(Email, Order.email_id == Email.id)
    if filters.get("email_type"):
        stmt = stmt.where(Email.detected_type == filters["email_type"])
    if filters.get("sender"):
        stmt = stmt.where(Email.sender.ilike(f"%{filters['sender']}%"))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(
                Email.subject.ilike(like),
                Email.sender.ilike(like),
                Email.body.ilike(like),
                Order.customer_detected_name.ilike(like),
            )
        )

    sort = filters.get("sort") or "date_desc"
    sort_map = {
        "date_asc": Order.created_at.asc(),
        "date_desc": Order.created_at.desc(),
        "score_asc": Order.score.asc(),
        "score_desc": Order.score.desc(),
        "status_asc": Order.status.asc(),
        "status_desc": Order.status.desc(),
        "customer_asc": Order.customer_detected_name.asc(),
        "customer_desc": Order.customer_detected_name.desc(),
        "sender_asc": Email.sender.asc(),
        "sender_desc": Email.sender.desc(),
        "subject_asc": Email.subject.asc(),
        "subject_desc": Email.subject.desc(),
        "type_asc": Email.detected_type.asc(),
        "type_desc": Email.detected_type.desc(),
    }
    stmt = stmt.options(
        joinedload(Order.email).selectinload(Email.attachments) if filters.get("has_pdf") else joinedload(Order.email),
        selectinload(Order.customer),
        selectinload(Order.validated_customer),
    ).order_by(sort_map.get(sort, Order.created_at.desc()), Order.id.asc())
    orders = db.scalars(stmt).unique().all()
    _load_order_conversations(db, orders, company_id)
    line_metrics_by_order = _load_order_line_metrics(db, company_id, [order.id for order in orders])

    scoring_filter = filters.get("scoring_category")
    if scoring_filter and scoring_filter != "all":
        if scoring_filter == "blocked":
            orders = [
                order
                for order in orders
                if order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) == "blocked"
            ]
        else:
            orders = [order for order in orders if scoring_category(order.score, settings) == scoring_filter]
    orders = filter_orders_for_operation(orders, settings, filters, line_metrics_by_order)
    agent_status = filters.get("agent_status")
    if agent_status and agent_status != "all":
        orders = [
            order
            for order in orders
            if (email_agent_status(order.email, order) if order.email else "order_detected") == agent_status
        ]
    if filters.get("has_attachments"):
        want_attachments = filters["has_attachments"] == "yes"
        orders = [order for order in orders if bool(order.email and order.email.has_attachments) == want_attachments]
    if filters.get("reason"):
        reason = filters["reason"].lower()
        orders = [
            order
            for order in orders
            if reason in order_issue_summary(order, settings, line_metrics_by_order=line_metrics_by_order).lower()
            or reason in str(order.status or "").lower()
        ]

    page_indexes, pagination = _order_view_pagination(
        len(orders),
        int(filters.get("page") or 1),
        int(filters.get("page_size") or 25),
    )
    customers = tuple(
        db.scalars(
            select(Customer)
            .where(Customer.company_id == company_id, Customer.deleted_at.is_(None))
            .order_by(Customer.fiscal_name)
        ).all()
    )
    statuses = tuple(
        db.scalars(
            select(Order.status)
            .where(Order.company_id == company_id)
            .distinct()
            .order_by(Order.status)
        ).all()
    )
    pdf_flags = {
        order.id: order_has_pdf(order) if filters.get("has_pdf") else bool(order.email and order.email.has_pdf)
        for order in orders
    }
    return {
        "settings": settings,
        "all_orders": orders,
        "orders": [orders[index] for index in page_indexes],
        "line_metrics": line_metrics_by_order,
        "pdf_flags": pdf_flags,
        "customers": customers,
        "statuses": statuses,
        "pagination": pagination,
    }


def orders_workbench_summary(order_view: dict, filters: dict) -> dict:
    """Render the canonical order dataset as workbench cards."""
    settings = order_view["settings"]
    line_metrics_by_order = order_view["line_metrics"]
    all_items = [
        order_workbench_item(
            order,
            settings,
            include_line_metrics=True,
            line_metrics_by_order=line_metrics_by_order,
        )
        for order in order_view["all_orders"]
    ]
    tab_counts = {
        "all": len(all_items),
        "not_processed": len([item for item in all_items if item["agent_status"] == "not_processed"]),
        "processed": len([item for item in all_items if item["agent_status"] in {"processed", "order_detected", "no_order", "doubtful"}]),
        "order_detected": len([item for item in all_items if item["agent_status"] == "order_detected"]),
        "attention": len([item for item in all_items if item["agent_status"] in {"doubtful", "error"} or item["scoring_category"] in {"doubtful", "blocked"} or str(item["order_status"]).startswith("error")]),
        "no_order": len([item for item in all_items if item["agent_status"] in {"no_order", "discarded"}]),
        "errors": len([item for item in all_items if item["agent_status"] == "error" or str(item["order_status"]).startswith("error")]),
    }

    items = all_items
    mode = filters.get("mode") or filters.get("tab") or "all"
    if mode == "not_processed":
        items = [item for item in items if item["agent_status"] == "not_processed"]
    elif mode == "processed":
        items = [item for item in items if item["agent_status"] in {"processed", "order_detected", "no_order", "doubtful"}]
    elif mode == "attention":
        items = [item for item in items if item["agent_status"] in {"doubtful", "error"} or item["scoring_category"] in {"doubtful", "blocked"} or str(item["order_status"]).startswith("error")]
    elif mode == "errors":
        items = [item for item in items if item["agent_status"] == "error" or str(item["order_status"]).startswith("error")]
    elif mode in {"no_order", "discarded_no_order"}:
        items = [item for item in items if item["agent_status"] in {"no_order", "discarded"}]

    agent_status = filters.get("agent_status")
    if agent_status and agent_status != "all":
        items = [item for item in items if item["agent_status"] == agent_status]
    scoring_filter = filters.get("scoring_category")
    if scoring_filter and scoring_filter != "all":
        items = [item for item in items if item["scoring_category"] == scoring_filter]
    if filters.get("has_attachments"):
        items = [item for item in items if item["has_attachments"] == (filters["has_attachments"] == "yes")]
    if filters.get("reason"):
        reason = filters["reason"].lower()
        items = [
            item
            for item in items
            if reason in (item.get("score_reason") or "").lower()
            or reason in (item.get("doubts_summary") or "").lower()
            or reason in (item.get("order_status_label") or "").lower()
        ]

    page_indexes, pagination = _order_view_pagination(
        len(items),
        int(filters.get("page") or 1),
        int(filters.get("page_size") or 25),
    )
    return {
        "tab_counts": tab_counts,
        "items": [items[index] for index in page_indexes],
        "pagination": pagination,
        "filters_applied": filters,
    }


def operational_summary(db: Session, company_id: int, filters: dict) -> dict:
    settings = get_or_create_settings(db, ScoringSettings, company_id)
    today = datetime.now(timezone.utc).date()
    orders_stmt = select(Order).where(Order.company_id == company_id).options(
        joinedload(Order.email),
        selectinload(Order.customer),
        selectinload(Order.validated_customer),
    )
    if filters.get("has_pdf"):
        orders_stmt = orders_stmt.options(
            joinedload(Order.email).selectinload(Email.attachments),
            selectinload(Order.conversation).selectinload(Conversation.messages).selectinload(InboundMessage.attachments),
        )
    emails_stmt = select(Email).where(Email.company_id == company_id)
    if filters.get("quick_range") == "today":
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start)
        emails_stmt = emails_stmt.where(Email.received_at >= start)
    elif filters.get("quick_range") == "yesterday":
        y = today.fromordinal(today.toordinal() - 1)
        start = datetime.combine(y, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start, Order.created_at < end)
        emails_stmt = emails_stmt.where(Email.received_at >= start, Email.received_at < end)
    elif filters.get("quick_range") == "7d":
        start = datetime.combine(today.fromordinal(today.toordinal() - 6), datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start)
        emails_stmt = emails_stmt.where(Email.received_at >= start)
    elif not any(filters.get(k) for k in ("date_from", "date_to", "quick_range")):
        start = datetime.combine(today.fromordinal(today.toordinal() - 6), datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start)
        emails_stmt = emails_stmt.where(Email.received_at >= start)
    if filters.get("date_from"):
        dt = datetime.fromisoformat(filters["date_from"]).replace(tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= dt)
        emails_stmt = emails_stmt.where(Email.received_at >= dt)
    if filters.get("date_to"):
        dt = datetime.fromisoformat(f"{filters['date_to']}T23:59:59").replace(tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at <= dt)
        emails_stmt = emails_stmt.where(Email.received_at <= dt)
    if filters.get("customer_id"):
        cid = int(filters["customer_id"])
        orders_stmt = orders_stmt.where((Order.customer_id == cid) | (Order.validated_customer_id == cid))
    if filters.get("status"):
        orders_stmt = orders_stmt.where(Order.status == filters["status"])
    if filters.get("score_min"):
        orders_stmt = orders_stmt.where(Order.score >= float(filters["score_min"]))
    if filters.get("score_max"):
        orders_stmt = orders_stmt.where(Order.score <= float(filters["score_max"]))
    joined_email = False
    if filters.get("email_type"):
        orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id).where(Email.detected_type == filters["email_type"])
        emails_stmt = emails_stmt.where(Email.detected_type == filters["email_type"])
        joined_email = True
    if filters.get("sender"):
        if not joined_email:
            orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id)
            joined_email = True
        like = f"%{filters['sender']}%"
        orders_stmt = orders_stmt.where(Email.sender.ilike(like))
        emails_stmt = emails_stmt.where(Email.sender.ilike(like))
    if filters.get("search"):
        if not joined_email:
            orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id)
            joined_email = True
        like = f"%{filters['search']}%"
        orders_stmt = orders_stmt.where(or_(Email.subject.ilike(like), Order.customer_detected_name.ilike(like)))
        emails_stmt = emails_stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like)))
    orders = db.scalars(orders_stmt.order_by(Order.created_at.desc())).unique().all()
    if not filters.get("has_pdf"):
        _load_order_conversations(db, orders, company_id)
    line_metrics_by_order = _load_order_line_metrics(db, company_id, [order.id for order in orders])
    emails = db.scalars(emails_stmt.order_by(Email.received_at.desc())).all()
    if filters.get("scoring_category"):
        orders = [order for order in orders if scoring_category(order.score, settings) == filters["scoring_category"]]
    orders = filter_orders_for_operation(orders, settings, filters, line_metrics_by_order)
    items = [build_order_item(order, settings, include_line_metrics=True, line_metrics_by_order=line_metrics_by_order) for order in orders]
    items.sort(key=lambda item: (item["priority_rank"], -_safe_sort_timestamp(item.get("date"))))
    page, page_size = normalize_page(int(filters.get("page") or 1), int(filters.get("page_size") or 25))
    total_items = len(items)
    total_pages = ceil(total_items / page_size) if total_items else 0
    start = (page - 1) * page_size
    paged_items = items[start:start + page_size]
    distribution = {key: {"count": 0, "percentage": 0} for key in ["safe", "reviewable", "doubtful", "blocked", "without_score"]}
    for order in orders:
        op = order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order)
        key = "blocked" if op == "blocked" else scoring_category(order.score, settings)
        if key == "not_importable":
            key = "blocked"
        distribution[key]["count"] += 1
    denom = sum(v["count"] for v in distribution.values()) or 1
    for value in distribution.values():
        value["percentage"] = round(value["count"] * 100 / denom, 1)
    ready = [order for order in orders if order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) == "ready"]
    review = [order for order in orders if order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) == "review"]
    blocked = [order for order in orders if order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) == "blocked"]
    errors = [order for order in orders if order_operational_category(order, settings, line_metrics_by_order=line_metrics_by_order) == "error"]
    exported_today = [order for order in orders if order.status == "pedido_exportado" and order.exported_at and order.exported_at.date() == today]
    alerts = []
    if errors:
        alerts.append({"type": "error_exportacion", "level": "error", "message": f"{len(errors)} pedidos tienen errores de exportacion o procesamiento.", "action_label": "Reintentar", "url": "/?work_status=error"})
    product_issues = [order for order in orders if _order_line_metrics(order, line_metrics_by_order)["missing_product_count"] > 0]
    if product_issues:
        alerts.append({"type": "producto_no_encontrado", "level": "warning", "message": f"{len(product_issues)} pedidos tienen productos no encontrados.", "action_label": "Revisar", "url": "/?issue_type=producto_no_encontrado"})
    doubtful_emails = [email for email in emails if email.detected_type == "dudoso"]
    if doubtful_emails:
        alerts.append({"type": "correo_dudoso", "level": "warning", "message": f"{len(doubtful_emails)} correos estan clasificados como dudosos.", "action_label": "Revisar", "url": "/?email_type=dudoso"})
    ftp = get_or_create_settings(db, FTPSettings, company_id)
    if not ftp.host:
        alerts.append({"type": "ftp", "level": "warning", "message": "El FTP/SFTP no esta configurado. Los pedidos confirmados no podran enviarse.", "action_label": "Configurar", "url": "/settings"})
    llm = get_or_create_settings(db, LLMSettings, company_id)
    if not llm.agent_enabled or llm.provider == "disabled" or llm.agent_mode == "desactivado":
        alerts.append({"type": "agent", "level": "warning", "message": "El agente IA esta pausado. Los correos no se procesaran automaticamente.", "action_label": "Configurar", "url": "/settings#agent-ai"})
    elif not llm.api_key_encrypted:
        alerts.append({"type": "llm", "level": "info", "message": "Agente IA no configurado. Falta API key para procesar correos con IA real.", "action_label": "Configurar", "url": "/settings#agent-ai"})
    elif llm.last_test_ok is False:
        alerts.append({"type": "llm", "level": "warning", "message": "La ultima prueba del agente IA fallo. Revisa la conexion antes de procesar nuevos correos.", "action_label": "Revisar", "url": "/settings#agent-ai"})
    return {
        "summary": {
            "received_today": len(emails),
            "processed_today": len([email for email in emails if email.status != "pending"]),
            "orders_detected": len(orders),
            "ready_to_confirm": len(ready),
            "requires_review": len(review),
            "blocked": len(blocked),
            "exported_today": len(exported_today),
            "errors": len(errors),
            "non_order_emails": len([email for email in emails if email.detected_type == "no_pedido"]),
        },
        "scoring_distribution": distribution,
        "alerts": alerts,
        "work_queue": paged_items,
        "orders": orders,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
            "start_item": start + 1 if total_items else 0,
            "end_item": min(start + page_size, total_items),
            "allowed_page_sizes": (10, 25, 50, 100),
        },
        "filters_applied": filters,
    }


def workbench_summary(db: Session, company_id: int, filters: dict, *, include_metrics: bool = True) -> dict:
    settings = get_or_create_settings(db, ScoringSettings, company_id)
    mapped_filters = dict(filters)
    if filters.get("tab") and not filters.get("mode"):
        mapped_filters["mode"] = filters["tab"]
        filters = dict(filters)
        filters["mode"] = filters["tab"]
    if mapped_filters.get("scoring_category") == "blocked":
        mapped_filters["scoring_category"] = ""
    if filters.get("date_range"):
        mapped_filters["quick_range"] = {"today": "today", "yesterday": "yesterday", "7d": "7d"}.get(filters["date_range"], filters.get("quick_range", ""))
    if filters.get("customer_or_sender"):
        mapped_filters["search"] = filters["customer_or_sender"]
        mapped_filters["sender"] = filters["customer_or_sender"]
    if filters.get("order_status"):
        mapped_filters["status"] = filters["order_status"]
    if filters.get("agent_status"):
        status_map = {
            "order_detected": "",
            "no_order": "no_pedido",
            "doubtful": "dudoso",
            "error": "error",
        }
        if status_map.get(filters["agent_status"]):
            mapped_filters["email_type"] = status_map[filters["agent_status"]]

    order_load_options = [
        joinedload(Order.email),
        selectinload(Order.customer),
        selectinload(Order.validated_customer),
    ]
    if mapped_filters.get("has_pdf"):
        order_load_options.extend(
            [
                joinedload(Order.email).selectinload(Email.attachments),
                selectinload(Order.conversation).selectinload(Conversation.messages).selectinload(InboundMessage.attachments),
            ]
        )
    orders_stmt = select(Order).where(
        Order.company_id == company_id,
        Order.deleted_at.is_(None),
        Order.archived.is_(False),
    ).options(*order_load_options)
    today = datetime.now(timezone.utc).date()
    quick_range = mapped_filters.get("quick_range")
    if quick_range == "today":
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start)
    elif quick_range == "yesterday":
        y = today.fromordinal(today.toordinal() - 1)
        start = datetime.combine(y, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start, Order.created_at < end)
    elif quick_range == "7d":
        start = datetime.combine(today.fromordinal(today.toordinal() - 6), datetime.min.time(), tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= start)
    if mapped_filters.get("date_from"):
        orders_stmt = orders_stmt.where(Order.created_at >= datetime.fromisoformat(mapped_filters["date_from"]).replace(tzinfo=timezone.utc))
    if mapped_filters.get("date_to"):
        orders_stmt = orders_stmt.where(Order.created_at <= datetime.fromisoformat(f"{mapped_filters['date_to']}T23:59:59").replace(tzinfo=timezone.utc))
    if mapped_filters.get("customer_id"):
        cid = int(mapped_filters["customer_id"])
        orders_stmt = orders_stmt.where((Order.customer_id == cid) | (Order.validated_customer_id == cid))
    if mapped_filters.get("status"):
        orders_stmt = orders_stmt.where(Order.status == mapped_filters["status"])
    if mapped_filters.get("score_min"):
        orders_stmt = orders_stmt.where(Order.score >= float(mapped_filters["score_min"]))
    if mapped_filters.get("score_max"):
        orders_stmt = orders_stmt.where(Order.score <= float(mapped_filters["score_max"]))
    if mapped_filters.get("email_type"):
        orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id).where(Email.detected_type == mapped_filters["email_type"])
    if mapped_filters.get("sender"):
        like = f"%{mapped_filters['sender']}%"
        orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id).where(Email.sender.ilike(like))
    if mapped_filters.get("search"):
        like = f"%{mapped_filters['search']}%"
        orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id).where(or_(Email.subject.ilike(like), Order.customer_detected_name.ilike(like)))
    orders = db.scalars(orders_stmt.order_by(Order.created_at.desc())).unique().all()
    if not mapped_filters.get("has_pdf"):
        _load_order_conversations(db, orders, company_id)
    line_metrics_by_order = _load_order_line_metrics(db, company_id, [order.id for order in orders])
    if mapped_filters.get("scoring_category"):
        orders = [order for order in orders if scoring_category(order.score, settings) == mapped_filters["scoring_category"]]
    orders = filter_orders_for_operation(orders, settings, mapped_filters, line_metrics_by_order)
    include_line_details = include_metrics or bool(mapped_filters.get("issue_type") or mapped_filters.get("requires_review"))
    order_items = [
        order_workbench_item(order, settings, include_line_metrics=include_line_details, line_metrics_by_order=line_metrics_by_order)
        for order in orders
    ]
    order_email_ids = {item["email_id"] for item in order_items if item["email_id"]}

    emails_stmt = select(Email).where(Email.company_id == company_id)
    archived_order_email_ids = select(Order.email_id).where(
        Order.company_id == company_id,
        Order.email_id.is_not(None),
        Order.archived.is_(True),
    )
    emails_stmt = emails_stmt.where(~Email.id.in_(archived_order_email_ids))
    today = datetime.now(timezone.utc).date()
    date_range = filters.get("date_range") or filters.get("quick_range") or ""
    if date_range == "today":
        emails_stmt = emails_stmt.where(Email.received_at >= datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))
    elif date_range == "yesterday":
        y = today.fromordinal(today.toordinal() - 1)
        emails_stmt = emails_stmt.where(Email.received_at >= datetime.combine(y, datetime.min.time(), tzinfo=timezone.utc), Email.received_at < datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))
    elif date_range == "7d":
        emails_stmt = emails_stmt.where(Email.received_at >= datetime.combine(today.fromordinal(today.toordinal() - 6), datetime.min.time(), tzinfo=timezone.utc))
    if filters.get("date_from"):
        emails_stmt = emails_stmt.where(Email.received_at >= datetime.fromisoformat(filters["date_from"]).replace(tzinfo=timezone.utc))
    if filters.get("date_to"):
        emails_stmt = emails_stmt.where(Email.received_at <= datetime.fromisoformat(f"{filters['date_to']}T23:59:59").replace(tzinfo=timezone.utc))
    if filters.get("email_type"):
        emails_stmt = emails_stmt.where(Email.detected_type == filters["email_type"])
    search = filters.get("customer_or_sender") or filters.get("search")
    if search:
        like = f"%{search}%"
        emails_stmt = emails_stmt.where(or_(Email.sender.ilike(like), Email.subject.ilike(like), Email.body.ilike(like)))
    if order_email_ids:
        emails_stmt = emails_stmt.where(~Email.id.in_(order_email_ids))
    emails = db.scalars(emails_stmt.order_by(Email.received_at.desc())).unique().all()
    suggestion_maps = _customer_suggestion_maps(db, company_id) if emails else None
    email_items = []
    for email in emails:
        if email.id in order_email_ids:
            continue
        item = email_workbench_item(email)
        item["suggested_customer"] = suggest_customer_for_email(db, company_id, email, suggestion_maps=suggestion_maps)
        if item["suggested_customer"]:
            item["customer_name"] = item["suggested_customer"]
        email_items.append(item)
    items = order_items + email_items

    tab_counts = {
        "all": len(items),
        "not_processed": len([item for item in items if item["agent_status"] == "not_processed"]),
        "processed": len([item for item in items if item["agent_status"] in {"processed", "order_detected", "no_order", "doubtful"}]),
        "order_detected": len([item for item in items if item["agent_status"] == "order_detected"]),
        "attention": len([item for item in items if item["agent_status"] in {"doubtful", "error"} or item["scoring_category"] in {"doubtful", "blocked"} or str(item["order_status"]).startswith("error")]),
        "no_order": len([item for item in items if item["agent_status"] in {"no_order", "discarded"}]),
        "processed_no_order": len([item for item in items if item["agent_status"] == "no_order"]),
        "doubtful": len([item for item in items if item["agent_status"] == "doubtful" or item["scoring_category"] == "doubtful"]),
        "errors": len([item for item in items if item["agent_status"] == "error" or str(item["order_status"]).startswith("error")]),
        "discarded": len([item for item in items if item["agent_status"] == "discarded"]),
    }

    mode = filters.get("mode") or ""
    if mode == "not_processed":
        items = [item for item in items if item["agent_status"] == "not_processed"]
    elif mode == "processed":
        items = [item for item in items if item["agent_status"] in {"processed", "order_detected", "no_order", "doubtful"}]
    elif mode == "order_detected":
        items = [item for item in items if item["agent_status"] == "order_detected"]
    elif mode == "attention":
        items = [item for item in items if item["agent_status"] in {"doubtful", "error"} or item["scoring_category"] in {"doubtful", "blocked"} or str(item["order_status"]).startswith("error")]
    elif mode == "errors":
        items = [item for item in items if item["agent_status"] == "error" or str(item["order_status"]).startswith("error")]
    elif mode in {"no_order", "discarded_no_order"}:
        items = [item for item in items if item["agent_status"] in {"no_order", "discarded"}]
    elif mode in {"", "all"}:
        items = [
            item for item in items
            if item["agent_status"] in {"not_processed", "queued", "processing", "pending_reprocess", "doubtful", "error"}
            or item["scoring_category"] in {"reviewable", "doubtful", "blocked"}
            or item["order_status_label"] == "Listo para confirmar"
            or str(item["order_status"]).startswith("error")
        ]

    agent_status = filters.get("agent_status")
    if agent_status and agent_status != "all":
        items = [item for item in items if item["agent_status"] == agent_status]
    scoring = filters.get("scoring_category")
    if scoring and scoring != "all":
        items = [item for item in items if item["scoring_category"] == scoring]
    if filters.get("has_pdf"):
        items = [item for item in items if item["has_pdf"] == (filters["has_pdf"] == "yes")]
    if filters.get("has_attachments"):
        items = [item for item in items if item["has_attachments"] == (filters["has_attachments"] == "yes")]
    if filters.get("origin"):
        items = [item for item in items if filters["origin"].lower() in item["origin"].lower()]
    if filters.get("score_min"):
        items = [item for item in items if item["score"] is not None and item["score"] >= float(filters["score_min"])]
    if filters.get("score_max"):
        items = [item for item in items if item["score"] is not None and item["score"] <= float(filters["score_max"])]
    if filters.get("reason"):
        reason = filters["reason"].lower()
        items = [
            item for item in items
            if reason in (item.get("score_reason") or "").lower()
            or reason in (item.get("doubts_summary") or "").lower()
            or reason in (item.get("review_reasons") or "").lower()
            or reason in (item.get("order_status_label") or "").lower()
        ]

    items.sort(key=lambda item: (item["priority_rank"], -_safe_sort_timestamp(item.get("received_at"))))
    page, page_size = normalize_page(int(filters.get("page") or 1), int(filters.get("page_size") or 25))
    total_items = len(items)
    total_pages = ceil(total_items / page_size) if total_items else 0
    start = (page - 1) * page_size
    paged_items = items[start:start + page_size]
    payload = {
        "tab_counts": tab_counts,
        "items": paged_items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
            "start_item": start + 1 if total_items else 0,
            "end_item": min(start + page_size, total_items),
            "allowed_page_sizes": (10, 25, 50, 100),
        },
        "filters_applied": filters,
    }
    if include_metrics:
        scoring_distribution = {key: {"count": 0, "percentage": 0} for key in ["safe", "reviewable", "doubtful", "blocked", "without_score"]}
        processed_for_distribution = [item for item in items if item["agent_status"] in {"processed", "order_detected", "no_order", "doubtful"}]
        for item in processed_for_distribution:
            key = item["scoring_category"] if item["scoring_category"] in scoring_distribution else "blocked"
            scoring_distribution[key]["count"] += 1
        denom = sum(value["count"] for value in scoring_distribution.values()) or 1
        for value in scoring_distribution.values():
            value["percentage"] = round(value["count"] * 100 / denom, 1)
        summary = {
            "all": tab_counts["all"],
            "not_processed": len([item for item in items if item["agent_status"] == "not_processed"]),
            "processed": len([item for item in items if item["agent_status"] in {"processed", "order_detected", "no_order", "doubtful"}]),
            "orders_detected": len([item for item in items if item["order_id"]]),
            "order_detected": len([item for item in items if item["agent_status"] == "order_detected"]),
            "processed_no_order": len([item for item in items if item["agent_status"] == "no_order"]),
            "doubtful": len([item for item in items if item["agent_status"] == "doubtful" or item["scoring_category"] == "doubtful"]),
            "ready_to_confirm": len([item for item in items if item["order_status_label"] == "Listo para confirmar"]),
            "requires_review": len([item for item in items if item["scoring_category"] in {"reviewable", "doubtful", "blocked"}]),
            "errors": len([item for item in items if item["agent_status"] == "error" or str(item["order_status"]).startswith("error")]),
            "discarded": len([item for item in items if item["agent_status"] == "discarded"]),
        }
        payload.update(
            {
                "summary": summary,
                "scoring_distribution": scoring_distribution,
                "scoring_summary": {key: value["count"] for key, value in scoring_distribution.items()},
                "all_items": items,
            }
        )
    return payload


def dashboard_summary(db: Session, company_id: int, filters: dict) -> dict:
    settings = get_or_create_settings(db, ScoringSettings, company_id)
    orders_stmt = select(Order).where(Order.company_id == company_id).options(
        joinedload(Order.email),
        selectinload(Order.customer),
        selectinload(Order.validated_customer),
    )
    if filters.get("has_pdf"):
        orders_stmt = orders_stmt.options(
            joinedload(Order.email).selectinload(Email.attachments),
            selectinload(Order.conversation).selectinload(Conversation.messages).selectinload(InboundMessage.attachments),
        )
    emails_stmt = select(Email).where(Email.company_id == company_id)
    if filters.get("date_from"):
        dt = datetime.fromisoformat(filters["date_from"]).replace(tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at >= dt)
        emails_stmt = emails_stmt.where(Email.received_at >= dt)
    if filters.get("date_to"):
        dt = datetime.fromisoformat(f"{filters['date_to']}T23:59:59").replace(tzinfo=timezone.utc)
        orders_stmt = orders_stmt.where(Order.created_at <= dt)
        emails_stmt = emails_stmt.where(Email.received_at <= dt)
    if filters.get("customer_id"):
        cid = int(filters["customer_id"])
        orders_stmt = orders_stmt.where((Order.customer_id == cid) | (Order.validated_customer_id == cid))
    if filters.get("status"):
        orders_stmt = orders_stmt.where(Order.status == filters["status"])
        emails_stmt = emails_stmt.where(Email.status == filters["status"])
    if filters.get("email_type"):
        emails_stmt = emails_stmt.where(Email.detected_type == filters["email_type"])
        orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id).where(Email.detected_type == filters["email_type"])
        joined_email = True
    else:
        joined_email = False
    if filters.get("score_min"):
        orders_stmt = orders_stmt.where(Order.score >= float(filters["score_min"]))
    if filters.get("score_max"):
        orders_stmt = orders_stmt.where(Order.score <= float(filters["score_max"]))
    if filters.get("sender"):
        like = f"%{filters['sender']}%"
        emails_stmt = emails_stmt.where(Email.sender.ilike(like))
        if not joined_email:
            orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id)
            joined_email = True
        orders_stmt = orders_stmt.where(Email.sender.ilike(like))
    if filters.get("search"):
        like = f"%{filters['search']}%"
        emails_stmt = emails_stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like)))
        if not joined_email:
            orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id)
            joined_email = True
        orders_stmt = orders_stmt.where(or_(Email.subject.ilike(like), Order.customer_detected_name.ilike(like)))
    orders = db.scalars(orders_stmt.order_by(Order.created_at.desc())).unique().all()
    if not filters.get("has_pdf"):
        _load_order_conversations(db, orders, company_id)
    order_email_ids = {order.email_id for order in orders if order.email_id}
    display_emails_stmt = emails_stmt.where(~Email.id.in_(order_email_ids)) if order_email_ids else emails_stmt
    emails = db.scalars(display_emails_stmt.order_by(Email.received_at.desc())).all()
    if filters.get("scoring_category"):
        orders = [order for order in orders if scoring_category(order.score, settings) == filters["scoring_category"]]
    processed_emails = db.scalar(
        select(func.count()).select_from(emails_stmt.where(Email.status != "pending").subquery())
    ) or 0
    total_scored = len(orders)
    distribution = {key: {"count": 0, "percentage": 0} for key in ["safe", "reviewable", "doubtful", "not_importable", "without_score"]}
    for order in orders:
        distribution[scoring_category(order.score, settings)]["count"] += 1
    total_distribution = sum(item["count"] for item in distribution.values()) or 1
    for item in distribution.values():
        item["percentage"] = round(item["count"] * 100 / total_distribution, 1)
    latest_items = []
    for order in orders:
        latest_items.append({
            "kind": "order",
            "id": order.id,
            "date": order.created_at,
            "sender": order.email.sender if order.email else "",
            "subject": order.email.subject if order.email else "",
            "customer": (order.validated_customer or order.customer).fiscal_name if (order.validated_customer or order.customer) else order.customer_detected_name,
            "type": order.email.detected_type if order.email else "pedido",
            "score": order.score,
            "category": scoring_category(order.score, settings),
            "category_label": category_label(scoring_category(order.score, settings)),
            "status": order.status,
            "url": f"/orders/{order.id}",
        })
    order_email_ids = {order.email_id for order in orders if order.email_id}
    for email in emails:
        if email.id in order_email_ids:
            continue
        latest_items.append({
            "kind": "email",
            "id": email.id,
            "date": email.received_at,
            "sender": email.sender,
            "subject": email.subject,
            "customer": "",
            "type": email.detected_type or "",
            "score": None,
            "category": "without_score",
            "category_label": category_label("without_score"),
            "status": email.status,
            "url": "/orders",
        })
    latest_items.sort(key=lambda item: _safe_sort_timestamp(item.get("date")), reverse=True)
    page, page_size = normalize_page(int(filters.get("page") or 1), int(filters.get("page_size") or 25))
    total_items = len(latest_items)
    total_pages = ceil(total_items / page_size) if total_items else 0
    start = (page - 1) * page_size
    paged_items = latest_items[start:start + page_size]
    average_score = round(sum(order.score for order in orders) / total_scored, 2) if total_scored else 0
    totals = {
        "processed_emails": processed_emails,
        "orders_detected": len(orders),
        "non_order_emails": len([email for email in emails if email.detected_type == "no_pedido"]),
        "doubtful_emails": len([email for email in emails if email.detected_type == "dudoso"]),
        "pending_review": len([order for order in orders if order.status in PENDING_ORDER_STATUSES]),
        "confirmed": len([order for order in orders if order.status in CONFIRMED_ORDER_STATUSES]),
        "exported": len([order for order in orders if order.status == "pedido_exportado"]),
        "errors": len([order for order in orders if order.status.startswith("error") or order.status in ERROR_ORDER_STATUSES]),
        "average_score": average_score,
    }
    return {
        "filters_applied": filters,
        "totals": totals,
        "scoring_distribution": distribution,
        "latest_items": paged_items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
            "start_item": start + 1 if total_items else 0,
            "end_item": min(start + page_size, total_items),
            "allowed_page_sizes": (10, 25, 50, 100),
        },
        "thresholds": {"safe": settings.safe_threshold, "reviewable": settings.review_threshold, "doubtful": settings.doubtful_threshold},
    }
