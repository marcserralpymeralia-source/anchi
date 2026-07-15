from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.dashboard.service import workbench_summary
from app.db.models import Customer, Email, Order, ScoringSettings
from app.dashboard.service import email_workbench_item, order_workbench_item, suggest_customer_for_email
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

    orders_stmt = select(Order).where(Order.company_id == user.company_id).options(
        selectinload(Order.email).selectinload(Email.attachments),
        selectinload(Order.customer),
        selectinload(Order.validated_customer),
    )
    emails_stmt = select(Email).where(Email.company_id == user.company_id).options(selectinload(Email.attachments))
    if start:
        orders_stmt = orders_stmt.where(Order.created_at >= start)
        emails_stmt = emails_stmt.where(Email.received_at >= start)
    if end:
        orders_stmt = orders_stmt.where(Order.created_at <= end)
        emails_stmt = emails_stmt.where(Email.received_at <= end)
    if customer_id and customer_id != "0":
        cid = int(customer_id)
        orders_stmt = orders_stmt.where((Order.customer_id == cid) | (Order.validated_customer_id == cid))
        emails_stmt = emails_stmt.where(Email.id.in_(select(Order.email_id).where(Order.company_id == user.company_id, (Order.customer_id == cid) | (Order.validated_customer_id == cid))))
    if search:
        like = f"%{search}%"
        orders_stmt = orders_stmt.join(Email, Order.email_id == Email.id).where(or_(Email.subject.ilike(like), Email.sender.ilike(like), Order.customer_detected_name.ilike(like)))
        emails_stmt = emails_stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like), Email.body.ilike(like)))

    orders = db.scalars(orders_stmt.order_by(Order.created_at.desc())).unique().all()
    emails = db.scalars(emails_stmt.order_by(Email.received_at.desc())).all()
    order_email_ids = {order.email_id for order in orders if order.email_id}

    history_items: list[dict] = []
    for order in orders:
        item = order_workbench_item(order, scoring_settings)
        item.update({
            "kind_label": "Pedido",
            "date": _aware(order.created_at),
            "url": f"/orders/{order.id}",
            "secondary_url": f"/channels?tab=processed&date_range=30d&search={quote_plus(order.email.subject or order.email.sender or order.customer_detected_name or '')}" if order.email else "/orders",
            "title": item["customer_name"] or order.customer_detected_name or "Pedido",
            "subtitle": order.email.subject if order.email else "",
        })
        history_items.append(item)
    for email in emails:
        if email.id in order_email_ids:
            continue
        item = email_workbench_item(email)
        item.update({
            "kind_label": "Correo",
            "date": _aware(email.received_at),
            "url": f"/channels?tab=processed&date_range=30d&search={quote_plus(email.subject or email.sender or '')}",
            "secondary_url": f"/channels?tab=email&date_range=30d&search={quote_plus(email.sender or email.subject or '')}",
            "title": email.subject or "Correo sin asunto",
            "subtitle": email.sender,
            "customer_name": suggest_customer_for_email(db, user.company_id, email) or item["customer_name"],
        })
        history_items.append(item)

    if kind in {"orders", "emails"}:
        history_items = [item for item in history_items if item["kind"] == ("order" if kind == "orders" else "email")]

    all_items_for_tabs = list(history_items)
    terminal_order_statuses = {"pedido_confirmado", "pedido_exportado", "cerrado", "cancelado", "descartado"}
    if state == "current":
        history_items = [item for item in history_items if item["kind"] == "order" and item["order_status"] not in terminal_order_statuses]
    elif state == "review":
        history_items = [item for item in history_items if item["kind"] == "order" and (item["scoring_category"] in {"reviewable", "doubtful", "blocked"} or item["order_status"] in {"dudoso", "no_importable"})]
    elif state == "ready":
        history_items = [item for item in history_items if item["kind"] == "order" and item["order_status_label"] == "Listo para confirmar"]
    elif state == "confirmed":
        history_items = [item for item in history_items if item["kind"] == "order" and item["order_status"] in {"pedido_confirmado", "pedido_validado"}]
    elif state == "sent":
        history_items = [item for item in history_items if item["kind"] == "order" and item["order_status"] == "pedido_exportado"]
    elif state == "blocked":
        history_items = [item for item in history_items if item["kind"] == "order" and (item["scoring_category"] == "blocked" or item["order_status"] in {"dudoso", "no_importable"})]

    history_items.sort(key=lambda item: item["date"], reverse=True)
    page, page_size = normalize_page(page, page_size)
    total_items = len(history_items)
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    start_index = (page - 1) * page_size
    paged_items = history_items[start_index:start_index + page_size]

    summary = {
        "events": len(history_items),
        "orders": len([item for item in history_items if item["kind"] == "order"]),
        "emails": len([item for item in history_items if item["kind"] == "email"]),
        "review": len([item for item in history_items if item["scoring_category"] in {"reviewable", "doubtful", "blocked"} or item["agent_status"] in {"error", "doubtful"}]),
        "ready": len([item for item in history_items if item["order_status_label"] == "Listo para confirmar"]),
    }
    order_items = [item for item in all_items_for_tabs if item["kind"] == "order"]
    tab_counts = {
        "all": len(all_items_for_tabs),
        "current": len([item for item in order_items if item["order_status"] not in terminal_order_statuses]),
        "review": len([item for item in order_items if item["scoring_category"] in {"reviewable", "doubtful", "blocked"} or item["order_status"] in {"dudoso", "no_importable"}]),
        "ready": len([item for item in order_items if item["order_status_label"] == "Listo para confirmar"]),
        "confirmed": len([item for item in order_items if item["order_status"] in {"pedido_confirmado", "pedido_validado"}]),
        "sent": len([item for item in order_items if item["order_status"] == "pedido_exportado"]),
        "blocked": len([item for item in order_items if item["scoring_category"] == "blocked" or item["order_status"] in {"dudoso", "no_importable"}]),
    }
    customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name)).all()
    return templates.TemplateResponse(
        "history/list.html",
        {
            "request": request,
            "user": user,
            "summary": summary,
            "items": paged_items,
            "all_items": history_items,
            "tab_counts": tab_counts,
            "customers": customers,
            "filters": {"date_range": date_range, "date_from": date_from, "date_to": date_to, "kind": kind, "state": state, "customer_id": customer_id, "search": search},
            "pagination": {"page": page, "page_size": page_size, "total_items": total_items, "total_pages": total_pages, "has_previous": page > 1, "has_next": page < total_pages, "start_item": start_index + 1 if total_items else 0, "end_item": min(start_index + page_size, total_items), "allowed_page_sizes": (10, 25, 50, 100)},
        },
    )
