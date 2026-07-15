from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
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
from app.tenancy.database import get_tenant_db

router = APIRouter(tags=["pages"])


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
    active_mode = mode or tab or "all"
    filters = {"date_from": date_from, "date_to": date_to, "customer_id": customer_id, "status": status, "email_type": email_type, "score_min": score_min, "score_max": score_max, "scoring_category": scoring_category, "agent_status": agent_status, "date_range": date_range or quick_range or "7d", "customer_or_sender": customer_or_sender, "has_attachments": has_attachments, "order_status": order_status, "mode": active_mode, "tab": active_mode, "work_status": work_status, "quick_range": date_range or quick_range or "7d", "has_pdf": has_pdf, "requires_review": requires_review, "issue_type": issue_type, "origin": origin, "sender": sender, "search": search, "reason": reason, "page": page, "page_size": page_size}
    workbench = workbench_summary(db, user.company_id, filters, include_metrics=False)
    featured_process_item = workbench["items"][0] if workbench["items"] else None
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "workbench": workbench, "featured_process_item": featured_process_item, "filters": filters, "pagination": workbench["pagination"]})


@router.get("/pedidos")
@router.get("/history")
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

    base_parts = []
    if allowed_kind in {"all", "orders"}:
        base_parts.append(order_base_stmt)
    if allowed_kind in {"all", "emails"}:
        base_parts.append(email_base_stmt)

    base_union = union_all(*base_parts).subquery() if base_parts else None
    current_union = base_union
    if allowed_state != "all":
        current_parts = []
        if allowed_kind in {"all", "orders"}:
            current_parts.append(
                _history_order_rows_stmt(
                    user.company_id,
                    scoring_settings,
                    start=start,
                    end=end,
                    customer_id=customer_id,
                    search=search,
                    state=allowed_state,
                )
            )
        if allowed_kind in {"all", "emails"} and allowed_state == "all":
            current_parts.append(email_base_stmt)
        current_union = union_all(*current_parts).subquery() if current_parts else None

    page, page_size = normalize_page(page, page_size)
    start_index = (page - 1) * page_size

    tab_counts = {key: 0 for key in ("all", "current", "review", "ready", "confirmed", "sent", "blocked")}
    summary = {"events": 0, "orders": 0, "emails": 0, "review": 0, "ready": 0}
    paged_items: list[dict] = []
    total_items = 0
    total_pages = 0
    start_item = 0
    end_item = 0

    if base_union is not None:
        base_counts_row = db.execute(
            select(
                func.count().label("all_count"),
                func.coalesce(func.sum(case((base_union.c.kind == "order", 1), else_=0)), 0).label("orders"),
                func.coalesce(func.sum(case((base_union.c.kind == "email", 1), else_=0)), 0).label("emails"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    base_union.c.kind == "order",
                                    base_union.c.order_status.notin_(TERMINAL_ORDER_STATUSES),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("current"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    base_union.c.kind == "order",
                                    or_(
                                        base_union.c.order_state == "blocked",
                                        base_union.c.scoring_category.in_(("reviewable", "doubtful")),
                                        base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES)),
                                    ),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("review"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                or_(
                                    and_(
                                        base_union.c.kind == "order",
                                        or_(
                                            base_union.c.order_state == "blocked",
                                            base_union.c.scoring_category.in_(("reviewable", "doubtful")),
                                            base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES)),
                                        ),
                                    ),
                                    and_(base_union.c.kind == "email", base_union.c.agent_status.in_(("error", "doubtful"))),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("summary_review"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(base_union.c.kind == "order", base_union.c.order_state == "ready"),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("ready"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(base_union.c.kind == "order", base_union.c.order_status.in_(("pedido_confirmado", "pedido_validado"))),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("confirmed"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(base_union.c.kind == "order", base_union.c.order_status == "pedido_exportado"),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("sent"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    base_union.c.kind == "order",
                                    or_(base_union.c.order_state == "blocked", base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES))),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("blocked"),
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

    if current_union is not None and current_union is not base_union:
        summary_row = db.execute(
            select(
                func.count().label("events"),
                func.coalesce(func.sum(case((current_union.c.kind == "order", 1), else_=0)), 0).label("orders"),
                func.coalesce(func.sum(case((current_union.c.kind == "email", 1), else_=0)), 0).label("emails"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                or_(
                                    and_(
                                        current_union.c.kind == "order",
                                        or_(
                                            current_union.c.order_state == "blocked",
                                            current_union.c.scoring_category.in_(("reviewable", "doubtful")),
                                            current_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES)),
                                        ),
                                    ),
                                    and_(current_union.c.kind == "email", current_union.c.agent_status.in_(("error", "doubtful"))),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("review"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(current_union.c.kind == "order", current_union.c.order_state == "ready"),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("ready"),
            ).select_from(current_union)
        ).one()
        summary_values = summary_row._mapping
        summary = {
            "events": int(summary_values["events"] or 0),
            "orders": int(summary_values["orders"] or 0),
            "emails": int(summary_values["emails"] or 0),
            "review": int(summary_values["review"] or 0),
            "ready": int(summary_values["ready"] or 0),
        }
    elif current_union is base_union and base_union is not None:
        summary = {
            "events": int(base_counts["all_count"] or 0),
            "orders": int(base_counts["orders"] or 0),
            "emails": int(base_counts["emails"] or 0),
            "review": int(base_counts["summary_review"] or 0),
            "ready": int(base_counts["ready"] or 0),
        }

        total_items = summary["events"]
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

        order_email_ids = {order.email_id for order in orders_by_id.values() if order.email_id}
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
                        "secondary_url": f"/channels?tab=processed&date_range=30d&search={quote_plus(order.email.subject or order.email.sender or order.customer_detected_name or '')}" if order.email else "/orders",
                        "title": item["customer_name"] or order.customer_detected_name or "Pedido",
                        "subtitle": order.email.subject if order.email else "",
                    }
                )
                paged_items.append(item)
            else:
                email = emails_by_id.get(row.item_id)
                if not email:
                    continue
                if email.id in order_email_ids:
                    continue
                item = email_workbench_item(email)
                item.update(
                    {
                        "kind_label": "Correo",
                        "date": _aware(email.received_at),
                        "url": f"/channels?tab=processed&date_range=30d&search={quote_plus(email.subject or email.sender or '')}",
                        "secondary_url": f"/channels?tab=email&date_range=30d&search={quote_plus(email.sender or email.subject or '')}",
                        "title": email.subject or "Correo sin asunto",
                        "subtitle": email.sender,
                        "customer_name": suggest_customer_for_email(db, user.company_id, email, suggestion_maps=suggestion_maps) or item["customer_name"],
                    }
                )
                paged_items.append(item)

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
            "filters": {"date_range": date_range, "date_from": date_from, "date_to": date_to, "kind": kind, "state": state, "customer_id": customer_id, "search": search},
            "pagination": {"page": page, "page_size": page_size, "total_items": total_items, "total_pages": total_pages, "has_previous": page > 1, "has_next": page < total_pages, "start_item": start_item, "end_item": end_item, "allowed_page_sizes": (10, 25, 50, 100)},
        },
    )
