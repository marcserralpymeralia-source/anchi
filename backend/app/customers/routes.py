from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.templating import templates
from app.core.pagination import normalize_page
from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.db.models import Customer, CustomerContactPoint, User
from app.databases.service import build_customer_context, customer_knowledge_overview, build_databases_context
from app.master_data.service import upsert_customer
from app.imports.service import import_customers, read_table
from app.logs.service import log_action

router = APIRouter(prefix="/customers", tags=["customers"])


_DELETE_ROLES = {"Administrador", "Superadmin", "Owner", "Propietario"}
_FINAL_ORDER_STATUSES = {"pedido_confirmado", "pedido_exportado", "no_pedido", "descartado", "deleted", "archived_deleted"}


def _can_delete(user: TenantUser) -> bool:
    return user.role.name in _DELETE_ROLES


def _customer_has_open_orders(db: Session, company_id: int, customer_id: int) -> bool:
    from app.db.models import Order

    return bool(
        db.scalar(
            select(Order.id).where(
                Order.company_id == company_id,
                or_(Order.customer_id == customer_id, Order.validated_customer_id == customer_id),
                Order.status.notin_(_FINAL_ORDER_STATUSES),
                Order.deleted_at.is_(None),
            ).limit(1)
        )
    )


def _soft_delete_customer(db: Session, customer: Customer, user: TenantUser) -> None:
    customer.status = "deleted"
    customer.company_inactive = True
    customer.deleted_at = datetime.now(timezone.utc)
    customer.deleted_by = user.id
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="customer.delete", entity_type="customer", entity_id=customer.id, message="Cliente eliminado")


def _matches_knowledge_status(card: dict, status: str) -> bool:
    if status in {"", "all"}:
        return True
    return card.get("status_key") == status


def _matches_knowledge_query(card: dict, q: str) -> bool:
    if not q:
        return True
    haystack = " ".join(
        [
            card.get("name", ""),
            card.get("code", ""),
            card.get("commercial_name", ""),
            card.get("primary_email", ""),
            card.get("primary_domain", ""),
            card.get("primary_endpoint", ""),
            " ".join(card.get("aliases", [])),
            " ".join(card.get("domains", [])),
            card.get("notes", ""),
        ]
    ).lower()
    return q.lower() in haystack


def _paginate_cards(cards: list[dict], page: int, page_size: int) -> tuple[list[dict], dict]:
    page, page_size = normalize_page(page, page_size)
    total_items = len(cards)
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    if total_pages and page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    page_items = cards[offset:offset + page_size]
    return page_items, {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
        "start_item": offset + 1 if total_items else 0,
        "end_item": min(offset + page_size, total_items),
        "allowed_page_sizes": (10, 25, 50, 100),
    }


def _normalize_customer_section(section: str) -> str:
    if section in {"summary", "articles", "conditions", "import"}:
        return section
    if section in {"documents", "importer", "transformer", "optimized"}:
        return "import"
    if section in {"comments", "history", "rules", "suggestions"}:
        return "summary"
    return "summary"


def _iter_values(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").replace(";", ",").split(",") if item.strip()]


def _upsert_contact_point(
    db: Session,
    *,
    company_id: int,
    customer_id: int,
    type: str,
    value: str,
    is_primary: bool = False,
    label: str | None = None,
    contact_name: str | None = None,
    contact_role: str | None = None,
    source: str = "manual",
) -> None:
    normalized = value.strip().lower()
    existing = db.scalar(
        select(CustomerContactPoint).where(
            CustomerContactPoint.company_id == company_id,
            CustomerContactPoint.customer_id == customer_id,
            CustomerContactPoint.type == type,
            CustomerContactPoint.value == normalized,
        )
    )
    now_point = datetime.now(timezone.utc)
    if existing:
        existing.label = label
        existing.contact_name = contact_name
        existing.contact_role = contact_role
        existing.is_primary = is_primary
        existing.active = True
        existing.source = source
        existing.last_seen_at = now_point
        existing.updated_at = now_point
        return
    db.add(
        CustomerContactPoint(
            company_id=company_id,
            customer_id=customer_id,
            type=type,
            value=normalized,
            label=label,
            contact_name=contact_name,
            contact_role=contact_role,
            is_primary=is_primary,
            active=True,
            confidence=0.85 if is_primary else 0.65,
            source=source,
            first_seen_at=now_point,
            last_seen_at=now_point,
        )
    )


def _sync_customer_contact_points(
    db: Session,
    *,
    customer: Customer,
    emails: str = "",
    phones: str = "",
    domains: str = "",
    aliases: str = "",
) -> None:
    db.query(CustomerContactPoint).filter(CustomerContactPoint.customer_id == customer.id).delete()
    email_values = [value for value in _iter_values(emails) if value]
    phone_values = [value for value in _iter_values(phones) if value]
    domain_values = [value.lower() for value in _iter_values(domains) if value]
    alias_values = [value for value in _iter_values(aliases) if value]
    if customer.primary_email:
        _upsert_contact_point(db, company_id=customer.company_id, customer_id=customer.id, type="email", value=customer.primary_email, is_primary=True, label="principal", source="manual")
    if customer.phone:
        _upsert_contact_point(db, company_id=customer.company_id, customer_id=customer.id, type="phone", value=customer.phone, is_primary=True, label="principal", source="manual")
    for email_value in email_values:
        _upsert_contact_point(db, company_id=customer.company_id, customer_id=customer.id, type="email", value=email_value, is_primary=False, label="asociado", source="manual")
    for phone_value in phone_values:
        _upsert_contact_point(db, company_id=customer.company_id, customer_id=customer.id, type="phone", value=phone_value, is_primary=False, label="asociado", source="manual")
    for domain_value in domain_values:
        _upsert_contact_point(db, company_id=customer.company_id, customer_id=customer.id, type="domain", value=domain_value, is_primary=domain_value in domain_values[:1], label="dominio", source="manual")
    for alias_value in alias_values:
        _upsert_contact_point(db, company_id=customer.company_id, customer_id=customer.id, type="alias", value=alias_value, is_primary=False, label="alias", source="manual")


@router.get("")
def list_customers(
    request: Request,
    view: str = "list",
    selected_id: int = 0,
    section: str = "summary",
    q: str = "",
    status: str = "all",
    channel: str = "",
    knowledge_state: str = "",
    sort: str = "updated_desc",
    density: str = "comfortable",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    view_mode = view if view in {"list", "knowledge"} else "list"
    section_mode = _normalize_customer_section(section)
    list_context = build_databases_context(db, user.company_id, tab="customers", q=q, status=status, selected_id=selected_id, page=page, page_size=page_size)
    knowledge_all = customer_knowledge_overview(db, user.company_id)
    knowledge_filtered = [card for card in knowledge_all if _matches_knowledge_status(card, status) and _matches_knowledge_query(card, q)]
    if sort == "name_asc":
        knowledge_filtered.sort(key=lambda card: (card.get("name") or "").lower())
    elif sort == "name_desc":
        knowledge_filtered.sort(key=lambda card: (card.get("name") or "").lower(), reverse=True)
    elif sort == "code_asc":
        knowledge_filtered.sort(key=lambda card: (card.get("code") or "").lower())
    elif sort == "code_desc":
        knowledge_filtered.sort(key=lambda card: (card.get("code") or "").lower(), reverse=True)
    else:
        knowledge_filtered.sort(key=lambda card: (card.get("last_updated_sort") or 0, (card.get("name") or "").lower()), reverse=True)
    knowledge_cards, knowledge_pagination = _paginate_cards(knowledge_filtered, page, page_size)
    knowledge_by_id = {card["id"]: card for card in knowledge_all}
    selected_customer = build_customer_context(db, user.company_id, selected_id, limit=8) if selected_id else None
    if view_mode == "knowledge":
        pagination = knowledge_pagination
    else:
        pagination = list_context.get("pagination", knowledge_pagination)
    customers_with_knowledge = []
    for row in list_context.get("customers", []):
        knowledge = knowledge_by_id.get(row["id"], {})
        row = dict(row)
        row["knowledge_state"] = knowledge.get("knowledge_state", "Sin conocimiento")
        row["knowledge_documents"] = knowledge.get("documents", 0)
        row["knowledge_habitual_products"] = knowledge.get("habitual_products", 0)
        row["knowledge_suggestions"] = knowledge.get("suggestions", 0)
        row["knowledge_last_updated"] = knowledge.get("last_updated", row.get("last_order_at", ""))
        row["knowledge_last_indexed"] = knowledge.get("last_indexed", "--")
        row["knowledge_url"] = knowledge.get("knowledge_url", f"/customers?view=knowledge&selected_id={row['id']}")
        row["open_url"] = f"/customers?view=list&selected_id={row['id']}"
        customers_with_knowledge.append(row)
    if channel:
        customers_with_knowledge = [row for row in customers_with_knowledge if channel.lower() in (row.get("habitual_channel") or "").lower()]
    if knowledge_state:
        customers_with_knowledge = [row for row in customers_with_knowledge if row.get("knowledge_state") == {
            "sin_conocimiento": "Sin conocimiento",
            "pendiente_importar": "Pendiente de importar",
            "en_construccion": "En construcción",
            "actualizado": "Actualizado",
            "con_errores": "Con errores",
            "con_sugerencias": "Con sugerencias",
        }.get(knowledge_state, row.get("knowledge_state"))]
    return templates.TemplateResponse(
        "customers/list.html",
        {
            "request": request,
            "user": user,
            "view": view_mode,
            "selected_id": selected_id,
            "section": section_mode,
            "q": q,
            "status": status,
            "channel": channel,
            "knowledge_state": knowledge_state,
            "sort": sort,
            "density": density if density in {"comfortable", "compact"} else "comfortable",
            "knowledge_query": q,
            "knowledge_status": status,
            **list_context,
            "customers": customers_with_knowledge,
            "knowledge_cards": knowledge_cards,
            "knowledge_total": len(knowledge_filtered),
            "knowledge_pagination": knowledge_pagination,
            "pagination": pagination,
            "selected_customer": selected_customer,
        },
    )


@router.get("/{customer_id}/knowledge")
def customer_knowledge(
    customer_id: int,
    request: Request,
    section: str = "summary",
    q: str = "",
    status: str = "all",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    section_mode = _normalize_customer_section(section)
    selected_customer = build_customer_context(db, user.company_id, customer_id, limit=50)
    if not selected_customer:
        return RedirectResponse("/customers?view=knowledge", status_code=303)
    return templates.TemplateResponse(
        "customers/knowledge.html",
        {
            "request": request,
            "user": user,
            "customer_context": selected_customer,
            "section": section_mode,
            "q": q,
            "status": status,
        },
    )


@router.post("")
def save_customer(
    id: int = Form(0),
    code: str = Form(...),
    fiscal_name: str = Form(...),
    tax_id: str = Form(""),
    delegation: str = Form(""),
    phone: str = Form(""),
    city: str = Form(""),
    province: str = Form(""),
    assigned_salesperson: str = Form(""),
    accounting_code: str = Form(""),
    company_inactive: bool = Form(False),
    category: str = Form(""),
    commercial_name: str = Form(""),
    primary_email: str = Form(""),
    associated_emails: str = Form(""),
    associated_phones: str = Form(""),
    domains: str = Form(""),
    aliases: str = Form(""),
    status: str = Form("active"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    customer_data = {
        "code": code,
        "fiscal_name": fiscal_name,
        "tax_id": tax_id,
        "delegation": delegation,
        "phone": phone,
        "city": city,
        "province": province,
        "assigned_salesperson": assigned_salesperson,
        "accounting_code": accounting_code,
        "company_inactive": "on" if company_inactive else "",
        "category": category,
        "commercial_name": commercial_name,
        "primary_email": primary_email,
        "associated_emails": associated_emails,
        "associated_phones": associated_phones,
        "domains": domains,
        "aliases": aliases,
        "status": "inactive" if company_inactive else status,
    }
    customer = upsert_customer(
        db,
        company_id=user.company_id,
        data=customer_data,
        source="manual",
        actor_id=user.id,
        customer_id=id or None,
        conflict_policy="update_existing",
    ).entity
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="customer.save", entity_type="customer", entity_id=customer.id, message=f"Cliente guardado: {code}")
    return RedirectResponse("/customers?view=list", status_code=303)


@router.post("/import")
async def import_file(
    file: UploadFile | None = File(None),
    pasted_text: str = Form(""),
    encoding: str = Form("utf-8"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if file and file.filename:
        df = await read_table(file)
        filename = file.filename
    elif pasted_text.strip():
        from app.imports.service import read_table_from_bytes

        filename = "clientes.csv"
        df = read_table_from_bytes(pasted_text.encode(encoding), filename, encoding=encoding)
    else:
        raise HTTPException(status_code=400, detail="Debes adjuntar un archivo o pegar una tabla para importar clientes.")
    job = import_customers(db, company_id=user.company_id, filename=filename or "clientes", df=df)
    log_action(db, company_id=user.company_id, user=user, action="customers.import", entity_type="import", entity_id=job.id, message=f"Importados clientes: {job.rows_created} creados, {job.rows_updated} actualizados")
    return RedirectResponse("/customers?view=list", status_code=303)


@router.get("/import")
def import_file_legacy() -> RedirectResponse:
    return RedirectResponse("/customers?view=list", status_code=303)


@router.post("/{customer_id}/delete")
def delete_customer_post(customer_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return delete_customer(customer_id, request, db, user)


@router.delete("/{customer_id}")
def delete_customer(customer_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_delete(user):
        raise HTTPException(status_code=403, detail="Sin permisos para eliminar.")
    customer = db.get(Customer, customer_id)
    if not customer or customer.company_id != user.company_id or customer.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")
    if _customer_has_open_orders(db, user.company_id, customer_id):
        raise HTTPException(status_code=400, detail="Este cliente tiene pedidos activos. Cierra o reasigna los pedidos antes de eliminarlo.")
    _soft_delete_customer(db, customer, user)
    return RedirectResponse(request.headers.get("referer") or "/customers?view=list", status_code=303)


@router.post("/bulk-delete")
def bulk_delete_customers(ids: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_delete(user):
        raise HTTPException(status_code=403, detail="Sin permisos para eliminar.")
    raw_ids = [int(item) for item in ids.split(",") if item.strip().isdigit()]
    deleted = 0
    blocked = 0
    for customer in db.scalars(select(Customer).where(Customer.company_id == user.company_id, Customer.id.in_(raw_ids), Customer.deleted_at.is_(None))).all():
        if _customer_has_open_orders(db, user.company_id, customer.id):
            blocked += 1
            continue
        _soft_delete_customer(db, customer, user)
        deleted += 1
    return JSONResponse({"success": True, "deleted": deleted, "blocked": blocked, "message": "Clientes eliminados correctamente"})


@router.get("/{customer_id}/contact-points")
def list_contact_points(customer_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    customer = db.get(Customer, customer_id)
    if not customer or customer.company_id != user.company_id:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    points = db.scalars(
        select(CustomerContactPoint)
        .where(CustomerContactPoint.company_id == user.company_id, CustomerContactPoint.customer_id == customer_id)
        .order_by(CustomerContactPoint.is_primary.desc(), CustomerContactPoint.type.asc(), CustomerContactPoint.value.asc())
    ).all()
    return JSONResponse(
        [
            {
                "id": point.id,
                "type": point.type,
                "value": point.value,
                "label": point.label,
                "contact_name": point.contact_name,
                "contact_role": point.contact_role,
                "is_primary": point.is_primary,
                "active": point.active,
                "confidence": point.confidence,
                "source": point.source,
                "first_seen_at": point.first_seen_at.isoformat() if point.first_seen_at else None,
                "last_seen_at": point.last_seen_at.isoformat() if point.last_seen_at else None,
            }
            for point in points
        ]
    )


@router.post("/{customer_id}/contact-points")
def create_contact_point(
    customer_id: int,
    type: str = Form(...),
    value: str = Form(...),
    label: str = Form(""),
    contact_name: str = Form(""),
    contact_role: str = Form(""),
    is_primary: bool = Form(False),
    active: bool = Form(True),
    confidence: float = Form(0.75),
    source: str = Form("manual"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    customer = db.get(Customer, customer_id)
    if not customer or customer.company_id != user.company_id:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    normalized = value.strip().lower()
    conflict = db.scalar(
        select(CustomerContactPoint).where(
            CustomerContactPoint.company_id == user.company_id,
            CustomerContactPoint.type == type,
            CustomerContactPoint.value == normalized,
            CustomerContactPoint.customer_id != customer_id,
            CustomerContactPoint.active == True,  # noqa: E712
        )
    )
    if conflict and not is_primary:
        return JSONResponse(
            {"detail": "Existe un punto de contacto activo en otro cliente", "conflict_customer_id": conflict.customer_id},
            status_code=409,
        )
    point = CustomerContactPoint(
        company_id=user.company_id,
        customer_id=customer_id,
        type=type,
        value=normalized,
        label=label or None,
        contact_name=contact_name or None,
        contact_role=contact_role or None,
        is_primary=is_primary,
        active=active,
        confidence=confidence,
        source=source,
    )
    db.add(point)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="customer.contact_point.create", entity_type="customer", entity_id=customer_id, message=f"Punto de contacto añadido: {type} {value}")
    return RedirectResponse(f"/customers?view=knowledge&selected_id={customer_id}&section=summary", status_code=303)


@router.put("/{customer_id}/contact-points/{contact_point_id}")
def update_contact_point(
    customer_id: int,
    contact_point_id: int,
    type: str = Form(...),
    value: str = Form(...),
    label: str = Form(""),
    contact_name: str = Form(""),
    contact_role: str = Form(""),
    is_primary: bool = Form(False),
    active: bool = Form(True),
    confidence: float = Form(0.75),
    source: str = Form("manual"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    point = db.get(CustomerContactPoint, contact_point_id)
    customer = db.get(Customer, customer_id)
    if not customer or customer.company_id != user.company_id or not point or point.company_id != user.company_id or point.customer_id != customer_id:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    point.type = type
    point.value = value.strip().lower()
    point.label = label or None
    point.contact_name = contact_name or None
    point.contact_role = contact_role or None
    point.is_primary = is_primary
    point.active = active
    point.confidence = confidence
    point.source = source
    point.updated_at = datetime.now(timezone.utc)
    point.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="customer.contact_point.update", entity_type="customer", entity_id=customer_id, message=f"Punto de contacto actualizado: {type} {value}")
    return RedirectResponse(f"/customers?view=knowledge&selected_id={customer_id}&section=summary", status_code=303)


@router.delete("/{customer_id}/contact-points/{contact_point_id}")
def delete_contact_point(customer_id: int, contact_point_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    point = db.get(CustomerContactPoint, contact_point_id)
    customer = db.get(Customer, customer_id)
    if not customer or customer.company_id != user.company_id or not point or point.company_id != user.company_id or point.customer_id != customer_id:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    db.delete(point)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="customer.contact_point.delete", entity_type="customer", entity_id=customer_id, message=f"Punto de contacto eliminado: {point.type} {point.value}")
    return RedirectResponse(f"/customers?view=knowledge&selected_id={customer_id}&section=summary", status_code=303)
