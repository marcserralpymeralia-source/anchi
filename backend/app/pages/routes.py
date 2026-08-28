from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, case, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.dashboard.service import workbench_summary
from app.db.models import Customer, Email, Order, OrderLine, ScoringSettings
from app.dashboard.service import _customer_suggestion_maps, _load_order_line_metrics, email_workbench_item, order_workbench_item, suggest_customer_for_email
from app.orders.state import ERROR_ORDER_STATUSES, PENDING_ORDER_STATUSES, REVIEW_ORDER_STATUSES, TERMINAL_ORDER_STATUSES
from app.master.service import TenantUser
from app.settings.service import get_or_create_settings
from app.setup.service import get_setup_status, is_setup_operational
from app.tenancy.database import get_tenant_db

router = APIRouter(tags=["pages"])


HISTORY_EMAIL_CURRENT_STATUSES = (
    "not_processed",
    "pending",
    "queued",
    "processing",
    "pending_reprocess",
    "doubtful",
    "processed_doubtful",
    "review",
    "error",
    "processing_error",
)
HISTORY_EMAIL_REVIEW_STATUSES = (
    "doubtful",
    "processed_doubtful",
    "pending_reprocess",
    "review",
    "error",
    "processing_error",
)
HISTORY_EMAIL_READY_STATUSES = (
    "processed",
    "processed_order_detected",
    "order_detected",
    "matched",
)


def _empty_workbench_summary(filters: dict) -> dict:
    return {
        "tab_counts": {"all": 0, "not_processed": 0, "attention": 0, "processed": 0, "errors": 0, "no_order": 0},
        "items": [],
        "pagination": {
            "page": int(filters.get("page") or 1),
            "page_size": int(filters.get("page_size") or 25),
            "total_items": 0,
            "total_pages": 0,
            "has_next": False,
            "has_previous": False,
            "start_item": 0,
            "end_item": 0,
            "allowed_page_sizes": (10, 25, 50, 100),
        },
        "filters_applied": filters,
    }


def _normalize_featured_process_item(item: dict) -> dict:
    normalized = dict(item)
    normalized["date"] = normalized.get("date") or normalized.get("received_at")
    normalized["customer_name"] = (
        normalized.get("customer_name")
        or normalized.get("customer")
        or normalized.get("from_email")
        or normalized.get("sender")
        or "Cliente no identificado"
    )
    normalized["origin"] = normalized.get("origin") or normalized.get("channel") or "PDF"
    normalized["score"] = normalized.get("score") if normalized.get("score") is not None else 0
    normalized["category_label"] = normalized.get("category_label") or normalized.get("category") or "Sin analizar"
    normalized["subject"] = normalized.get("subject") or "Pedido compra"
    normalized["detail_url"] = normalized.get("detail_url") or (
        f"/orders/{normalized['order_id']}" if normalized.get("order_id") else f"/?focus=email-{normalized['email_id']}" if normalized.get("email_id") else "/"
    )
    return normalized


def _history_bounds(date_range: str, date_from: str, date_to: str) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(timezone.utc)
    if date_from or date_to:
        start = _aware(datetime.fromisoformat(date_from)) if date_from else None
        end = _aware(datetime.fromisoformat(f"{date_to}T23:59:59")) if date_to else None
        return start, end
    ranges = {
        "today": (datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc), None),
        "yesterday": (
            datetime.combine((now - timedelta(days=1)).date(), datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc),
        ),
        "7d": (now - timedelta(days=7), None),
        "30d": (now - timedelta(days=30), None),
        "90d": (now - timedelta(days=90), None),
        "365d": (now - timedelta(days=365), None),
    }
    return ranges.get(date_range, (None, None))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _history_scoring_category_expr(settings: ScoringSettings):
    return case(
        (Order.score.is_(None), literal("without_score")),
        (Order.score >= settings.safe_threshold, literal("safe")),
        (Order.score >= settings.review_threshold, literal("reviewable")),
        (Order.score >= settings.doubtful_threshold, literal("doubtful")),
        else_=literal("not_importable"),
    )


def _history_blocked_expr(settings: ScoringSettings, metrics):
    return or_(
        and_(settings.block_without_customer, Order.validated_customer_id.is_(None)),
        and_(settings.block_without_reference, func.coalesce(metrics.c.missing_product_count, 0) > 0),
        and_(settings.block_without_quantity, func.coalesce(metrics.c.invalid_quantity_count, 0) > 0),
        and_(settings.block_below_threshold, or_(Order.score.is_(None), Order.score < settings.doubtful_threshold)),
    )


def _history_order_state_expr(settings: ScoringSettings, metrics):
    scoring_category = _history_scoring_category_expr(settings)
    blocked_expr = _history_blocked_expr(settings, metrics)
    return case(
        (Order.status.in_(tuple(ERROR_ORDER_STATUSES)), literal("error")),
        (blocked_expr, literal("blocked")),
        (
            and_(scoring_category == "safe", Order.status.in_(tuple(PENDING_ORDER_STATUSES))),
            literal("ready"),
        ),
        (
            or_(scoring_category.in_(("reviewable", "doubtful")), Order.status.in_(tuple(REVIEW_ORDER_STATUSES))),
            literal("review"),
        ),
        (Order.status == "pedido_exportado", literal("exported")),
        else_=literal("normal"),
    )


def _history_order_metrics_subquery(company_id: int):
    return (
        select(
            OrderLine.order_id.label("order_id"),
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
        .where(OrderLine.company_id == company_id)
        .group_by(OrderLine.order_id)
        .subquery()
    )


def _history_order_rows_stmt(
    company_id: int,
    scoring_settings: ScoringSettings,
    *,
    start: datetime | None,
    end: datetime | None,
    customer_id: str,
    search: str,
    state: str,
):
    metrics = _history_order_metrics_subquery(company_id)
    scoring_category = _history_scoring_category_expr(scoring_settings)
    blocked_expr = _history_blocked_expr(scoring_settings, metrics)
    op_state = _history_order_state_expr(scoring_settings, metrics)
    stmt = (
        select(
            literal("order").label("kind"),
            Order.id.label("item_id"),
            Order.created_at.label("sort_date"),
            scoring_category.label("scoring_category"),
            op_state.label("order_state"),
            Order.status.label("order_status"),
            func.coalesce(metrics.c.line_count, 0).label("line_count"),
            func.coalesce(metrics.c.doubt_count, 0).label("doubt_count"),
            func.coalesce(metrics.c.missing_product_count, 0).label("missing_product_count"),
            func.coalesce(metrics.c.invalid_quantity_count, 0).label("invalid_quantity_count"),
            Order.customer_detected_name.label("customer_detected_name"),
            Order.score.label("score"),
            Order.validated_customer_id.label("validated_customer_id"),
            Order.customer_id.label("customer_id"),
            Order.conversation_id.label("conversation_id"),
            func.coalesce(Email.agent_status, literal("not_processed")).label("agent_status"),
            Email.subject.label("subject"),
            Email.sender.label("sender"),
        )
        .select_from(Order)
        .outerjoin(Email, Order.email_id == Email.id)
        .outerjoin(metrics, metrics.c.order_id == Order.id)
        .where(Order.company_id == company_id)
    )
    if start:
        stmt = stmt.where(Order.created_at >= start)
    if end:
        stmt = stmt.where(Order.created_at <= end)
    if customer_id and customer_id != "0":
        cid = int(customer_id)
        stmt = stmt.where((Order.customer_id == cid) | (Order.validated_customer_id == cid))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like), Order.customer_detected_name.ilike(like)))
    if state == "current":
        stmt = stmt.where(Order.status.not_in(tuple(TERMINAL_ORDER_STATUSES)))
    elif state == "review":
        stmt = stmt.where(
            or_(
                scoring_category.in_(("reviewable", "doubtful")),
                blocked_expr,
                Order.status.in_(tuple(REVIEW_ORDER_STATUSES)),
            )
        )
    elif state == "ready":
        stmt = stmt.where(and_(scoring_category == "safe", Order.status.in_(tuple(PENDING_ORDER_STATUSES))))
    elif state == "confirmed":
        stmt = stmt.where(Order.status.in_(("pedido_confirmado", "pedido_validado")))
    elif state == "sent":
        stmt = stmt.where(Order.status == "pedido_exportado")
    elif state == "blocked":
        stmt = stmt.where(or_(blocked_expr, Order.status.in_(tuple(REVIEW_ORDER_STATUSES))))
    return stmt


def _history_email_rows_stmt(
    company_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    customer_id: str,
    search: str,
):
    stmt = (
        select(
            literal("email").label("kind"),
            Email.id.label("item_id"),
            Email.received_at.label("sort_date"),
            literal("without_score").label("scoring_category"),
            literal("processed").label("order_state"),
            literal("").label("order_status"),
            literal(0).label("line_count"),
            literal(0).label("doubt_count"),
            literal(0).label("missing_product_count"),
            literal(0).label("invalid_quantity_count"),
            literal("").label("customer_detected_name"),
            literal(None).label("score"),
            literal(None).label("validated_customer_id"),
            literal(None).label("customer_id"),
            Email.conversation_id.label("conversation_id"),
            func.coalesce(Email.agent_status, literal("not_processed")).label("agent_status"),
            Email.subject.label("subject"),
            Email.sender.label("sender"),
        )
        .where(Email.company_id == company_id)
    )
    if start:
        stmt = stmt.where(Email.received_at >= start)
    if end:
        stmt = stmt.where(Email.received_at <= end)
    if customer_id and customer_id != "0":
        cid = int(customer_id)
        stmt = stmt.where(
            exists(
                select(1).where(
                    Order.company_id == company_id,
                    Order.email_id == Email.id,
                    or_(Order.customer_id == cid, Order.validated_customer_id == cid),
                )
            )
        )
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like), Email.body.ilike(like)))
    return stmt


@router.get("/")
def root_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    customer_id: str = "",
    status: str = "",
    email_type: str = "",
    score_min: str = "",
    score_max: str = "",
    scoring_category: str = "",
    agent_status: str = "",
    date_range: str = "7d",
    customer_or_sender: str = "",
    has_attachments: str = "",
    order_status: str = "",
    mode: str = "",
    tab: str = "",
    work_status: str = "",
    quick_range: str = "today",
    has_pdf: str = "",
    requires_review: str = "",
    issue_type: str = "",
    origin: str = "",
    sender: str = "",
    search: str = "",
    reason: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    return dashboard(
        request,
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        status=status,
        email_type=email_type,
        score_min=score_min,
        score_max=score_max,
        scoring_category=scoring_category,
        agent_status=agent_status,
        date_range=date_range,
        customer_or_sender=customer_or_sender,
        has_attachments=has_attachments,
        order_status=order_status,
        mode=mode,
        tab=tab,
        work_status=work_status,
        quick_range=quick_range,
        has_pdf=has_pdf,
        requires_review=requires_review,
        issue_type=issue_type,
        origin=origin,
        sender=sender,
        search=search,
        reason=reason,
        page=page,
        page_size=page_size,
        db=db,
        user=user,
    )


@router.get("/inicio")
def dashboard(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    customer_id: str = "",
    status: str = "",
    email_type: str = "",
    score_min: str = "",
    score_max: str = "",
    scoring_category: str = "",
    agent_status: str = "",
    date_range: str = "7d",
    customer_or_sender: str = "",
    has_attachments: str = "",
    order_status: str = "",
    mode: str = "",
    tab: str = "",
    work_status: str = "",
    quick_range: str = "today",
    has_pdf: str = "",
    requires_review: str = "",
    issue_type: str = "",
    origin: str = "",
    sender: str = "",
    search: str = "",
    reason: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if request.url.path == "/inicio":
        redirect_target = "/" if not request.url.query else f"/?{request.url.query}"
        return RedirectResponse(redirect_target, status_code=303)

    if not is_setup_operational(db, user.company_id):
        setup_status = get_setup_status(db, user.company_id)
        missing = [
            item["label"]
            for item in setup_status.steps
            if item["status"] not in {"Completado", "Opcional"}
        ]
        return templates.TemplateResponse(
            "setup/required.html",
            {
                "request": request,
                "user": user,
                "title": "Configuración pendiente",
                "setup_status": setup_status,
                "missing_steps": missing,
            },
        )

    active_mode = mode or tab or "all"
    filters = {"date_from": date_from, "date_to": date_to, "customer_id": customer_id, "status": status, "email_type": email_type, "score_min": score_min, "score_max": score_max, "scoring_category": scoring_category, "agent_status": agent_status, "date_range": date_range or quick_range or "7d", "customer_or_sender": customer_or_sender, "has_attachments": has_attachments, "order_status": order_status, "mode": active_mode, "tab": active_mode, "work_status": work_status, "quick_range": date_range or quick_range or "7d", "has_pdf": has_pdf, "requires_review": requires_review, "issue_type": issue_type, "origin": origin, "sender": sender, "search": search, "reason": reason, "page": page, "page_size": page_size}

    try:
        workbench = workbench_summary(db, user.company_id, filters, include_metrics=False)
    except Exception:
        workbench = _empty_workbench_summary(filters)

    featured_process_item = None
    if workbench["items"]:
        featured_process_item = _normalize_featured_process_item(workbench["items"][0])

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "workbench": workbench,
            "featured_process_item": featured_process_item,
            "filters": filters,
            "pagination": workbench["pagination"],
        },
    )
@router.get("/history")
def history_page(
    request: Request,
    date_range: str = "90d",
    date_from: str = "",
    date_to: str = "",
    kind: str = "all",
    state: str = "all",
    customer_id: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    selected_id: int | None = None,
    selected_kind: str = "email",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    return pedidos_page(
        request=request,
        date_range=date_range,
        date_from=date_from,
        date_to=date_to,
        kind=kind,
        state=state,
        customer_id=customer_id,
        search=search,
        page=page,
        page_size=page_size,
        selected_id=selected_id,
        selected_kind=selected_kind,
        db=db,
        user=user,
    )


@router.get("/pedidos")
def pedidos_page(
    request: Request,
    date_range: str = "90d",
    date_from: str = "",
    date_to: str = "",
    kind: str = "all",
    state: str = "all",
    customer_id: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    selected_id: int | None = None,
    selected_kind: str = "email",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    start, end = _history_bounds(date_range, date_from, date_to)
    scoring_settings = get_or_create_settings(db, ScoringSettings, user.company_id)
    allowed_kind = kind if kind in {"all", "orders", "emails"} else "all"
    allowed_state = state if state in {"all", "current", "review", "ready", "confirmed", "sent", "blocked"} else "all"
    suggestion_maps = _customer_suggestion_maps(db, user.company_id)

    order_base_stmt = _history_order_rows_stmt(
        user.company_id,
        scoring_settings,
        start=start,
        end=end,
        customer_id=customer_id,
        search=search,
        state="all",
    )
    email_base_stmt = _history_email_rows_stmt(
        user.company_id,
        start=start,
        end=end,
        customer_id=customer_id,
        search=search,
    )
    if allowed_kind == "all":
        email_base_stmt = email_base_stmt.where(
            ~exists(
                select(1).where(
                    Order.company_id == user.company_id,
                    Order.email_id == Email.id,
                )
            )
        )

    base_parts = []
    if allowed_kind in {"all", "orders"}:
        base_parts.append(order_base_stmt)
    if allowed_kind in {"all", "emails"}:
        base_parts.append(email_base_stmt)

    base_union = union_all(*base_parts).subquery() if base_parts else None

    page, page_size = normalize_page(page, page_size)
    start_index = (page - 1) * page_size

    tab_counts = {key: 0 for key in ("all", "current", "review", "ready", "confirmed", "sent", "blocked")}
    summary = {"events": 0, "orders": 0, "emails": 0, "review": 0, "ready": 0}
    paged_items: list[dict] = []
    total_items = 0
    total_pages = 0
    start_item = 0
    end_item = 0
    current_union = None

    if base_union is not None:
        pred_current = or_(
            and_(base_union.c.kind == "order", base_union.c.order_status.notin_(TERMINAL_ORDER_STATUSES)),
            and_(base_union.c.kind == "email", base_union.c.agent_status.in_(HISTORY_EMAIL_CURRENT_STATUSES)),
        )
        pred_review = or_(
            and_(
                base_union.c.kind == "order",
                or_(
                    base_union.c.order_state == "blocked",
                    base_union.c.scoring_category.in_(("reviewable", "doubtful")),
                    base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES)),
                ),
            ),
            and_(base_union.c.kind == "email", base_union.c.agent_status.in_(HISTORY_EMAIL_REVIEW_STATUSES)),
        )
        pred_ready = or_(
            and_(base_union.c.kind == "order", base_union.c.order_state == "ready"),
            and_(base_union.c.kind == "email", base_union.c.agent_status.in_(HISTORY_EMAIL_READY_STATUSES)),
        )
        pred_confirmed = and_(base_union.c.kind == "order", base_union.c.order_status.in_(("pedido_confirmado", "pedido_validado")))
        pred_sent = and_(base_union.c.kind == "order", base_union.c.order_status == "pedido_exportado")
        pred_blocked = and_(base_union.c.kind == "order", or_(base_union.c.order_state == "blocked", base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES))))

        base_counts_row = db.execute(
            select(
                func.count().label("all_count"),
                func.coalesce(func.sum(case((base_union.c.kind == "order", 1), else_=0)), 0).label("orders"),
                func.coalesce(func.sum(case((base_union.c.kind == "email", 1), else_=0)), 0).label("emails"),
                func.coalesce(func.sum(case((pred_current, 1), else_=0)), 0).label("current"),
                func.coalesce(func.sum(case((pred_review, 1), else_=0)), 0).label("review"),
                func.coalesce(func.sum(case((pred_ready, 1), else_=0)), 0).label("ready"),
                func.coalesce(func.sum(case((pred_confirmed, 1), else_=0)), 0).label("confirmed"),
                func.coalesce(func.sum(case((pred_sent, 1), else_=0)), 0).label("sent"),
                func.coalesce(func.sum(case((pred_blocked, 1), else_=0)), 0).label("blocked"),
            ).select_from(base_union)
        ).one()
        base_counts = base_counts_row._mapping
        tab_counts = {
            "all": int(base_counts["all_count"] or 0),
            "current": int(base_counts["current"] or 0),
            "review": int(base_counts["review"] or 0),
            "ready": int(base_counts["ready"] or 0),
            "confirmed": int(base_counts["confirmed"] or 0),
            "sent": int(base_counts["sent"] or 0),
            "blocked": int(base_counts["blocked"] or 0),
        }

        state_preds = {
            "current": pred_current,
            "review": pred_review,
            "ready": pred_ready,
            "confirmed": pred_confirmed,
            "sent": pred_sent,
            "blocked": pred_blocked,
        }

        if allowed_state != "all" and allowed_state in state_preds:
            current_union = select(base_union).where(state_preds[allowed_state]).subquery()
            total_items = tab_counts[allowed_state]
        else:
            current_union = base_union
            total_items = tab_counts["all"]

        summary = {
            "events": total_items,
            "orders": int(base_counts["orders"] or 0),
            "emails": int(base_counts["emails"] or 0),
            "review": int(base_counts["review"] or 0),
            "ready": int(base_counts["ready"] or 0),
        }

        total_pages = (total_items + page_size - 1) // page_size if total_items else 0
        paged_rows = db.execute(
            select(current_union).order_by(current_union.c.sort_date.desc(), current_union.c.kind.asc(), current_union.c.item_id.desc()).offset(start_index).limit(page_size)
        ).all()
        start_item = start_index + 1 if total_items else 0
        end_item = min(start_index + page_size, total_items)

        order_ids = [row.item_id for row in paged_rows if row.kind == "order"]
        email_ids = [row.item_id for row in paged_rows if row.kind == "email"]
        orders_by_id = {}
        emails_by_id = {}
        line_metrics_by_order = _load_order_line_metrics(db, user.company_id, order_ids) if order_ids else {}
        if order_ids:
            orders = db.scalars(
                select(Order)
                .where(Order.company_id == user.company_id, Order.id.in_(order_ids))
                .options(
                    selectinload(Order.email).selectinload(Email.attachments),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                )
            ).unique().all()
            orders_by_id = {order.id: order for order in orders}
        if email_ids:
            emails = db.scalars(
                select(Email)
                .where(Email.company_id == user.company_id, Email.id.in_(email_ids))
                .options(selectinload(Email.attachments))
            ).all()
            emails_by_id = {email.id: email for email in emails}

        for row in paged_rows:
            if row.kind == "order":
                order = orders_by_id.get(row.item_id)
                if not order:
                    continue
                item = order_workbench_item(order, scoring_settings, line_metrics_by_order=line_metrics_by_order)
                item.update(
                    {
                        "kind_label": "Pedido",
                        "date": _aware(order.created_at),
                        "url": f"/orders/{order.id}",
                        "secondary_url": f"/?tab=processed&date_range=30d&search={quote_plus(order.email.subject or order.email.sender or order.customer_detected_name or '')}" if order.email else "/orders",
                        "title": item["customer_name"] or order.customer_detected_name or "Pedido",
                        "subtitle": order.email.subject if order.email else "",
                    }
                )
                paged_items.append(item)
            else:
                email = emails_by_id.get(row.item_id)
                if not email:
                    continue
                item = email_workbench_item(email)
                item.update(
                    {
                        "kind_label": "Correo",
                        "date": _aware(email.received_at),
                        "url": f"/?tab=processed&date_range=30d&search={quote_plus(email.subject or email.sender or '')}",
                        "secondary_url": f"/?tab=email&date_range=30d&search={quote_plus(email.sender or email.subject or '')}",
                        "title": email.subject or "Correo sin asunto",
                        "subtitle": email.sender,
                        "customer_name": suggest_customer_for_email(db, user.company_id, email, suggestion_maps=suggestion_maps) or item["customer_name"],
                    }
                )
                paged_items.append(item)

    selected_email = None
    selected_order = None
    selected_item = None
    if selected_id:
        if selected_kind == "order":
            selected_order = db.scalar(
                select(Order)
                .where(Order.company_id == user.company_id, Order.id == selected_id)
                .options(
                    selectinload(Order.email).selectinload(Email.attachments),
                    selectinload(Order.lines),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                )
            )
            if selected_order and selected_order.email:
                selected_email = selected_order.email
        else:
            selected_email = db.scalar(
                select(Email)
                .where(Email.company_id == user.company_id, Email.id == selected_id)
                .options(selectinload(Email.attachments))
            )
            if selected_email:
                selected_order = db.scalar(
                    select(Order)
                    .where(Order.company_id == user.company_id, Order.email_id == selected_email.id)
                    .options(
                        selectinload(Order.lines),
                        selectinload(Order.customer),
                        selectinload(Order.validated_customer),
                    )
                )

    customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name)).all()
    return templates.TemplateResponse(
        "history/list.html",
        {
            "request": request,
            "user": user,
            "summary": summary,
            "items": paged_items,
            "all_items": paged_items,
            "tab_counts": tab_counts,
            "customers": customers,
            "selected_id": selected_id,
            "selected_kind": selected_kind,
            "selected_email": selected_email,
            "selected_order": selected_order,
            "selected_item": selected_item,
            "filters": {"date_range": date_range, "date_from": date_from, "date_to": date_to, "kind": kind, "state": state, "customer_id": customer_id, "search": search},
            "pagination": {"page": page, "page_size": page_size, "total_items": total_items, "total_pages": total_pages, "has_previous": page > 1, "has_next": page < total_pages, "start_item": start_item, "end_item": end_item, "allowed_page_sizes": (10, 25, 50, 100)},
        },
    )


@router.get("/history/pane/{kind}/{item_id}")
def history_detail_pane(
    kind: str,
    item_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    email = None
    order = None
    item = None
    if kind == "order":
        order = db.scalar(
            select(Order)
            .where(Order.company_id == user.company_id, Order.id == item_id)
            .options(
                selectinload(Order.email).selectinload(Email.attachments),
                selectinload(Order.lines),
                selectinload(Order.customer),
                selectinload(Order.validated_customer),
            )
        )
        if order and order.email:
            email = order.email
            item = email_workbench_item(email)
    else:
        email = db.scalar(
            select(Email)
            .where(Email.company_id == user.company_id, Email.id == item_id)
            .options(selectinload(Email.attachments))
        )
        if email:
            order = db.scalar(
                select(Order)
                .where(Order.company_id == user.company_id, Order.email_id == email.id)
                .options(
                    selectinload(Order.lines),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                )
            )
            item = email_workbench_item(email)
            if order:
                item["order_id"] = order.id
                item["order_status"] = order.status
                item["order_status_label"] = order.status
                item["score"] = order.score

    return templates.TemplateResponse(
        "history/_mail_detail_pane.html",
        {
            "request": request,
            "user": user,
            "email": email,
            "order": order,
            "item": item,
        },
    )
