from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.pagination import normalize_page, paginate
from app.db.models import Customer, CustomerAlias, CustomerContact, CustomerContactPoint, CustomerDomain, CustomerProductKnowledge, LearnedAlias, ManualCorrection, Order, OrderLine, Product, ProductAlias, RagCase, RagDocument


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "--"


def _customer_related_stats(db: Session, company_id: int, customer_ids: list[int]) -> tuple[dict[int, int], dict[int, datetime | None], dict[int, int]]:
    if not customer_ids:
        return {}, {}, {}
    ordered_counts = defaultdict(int)
    latest = {}
    line_counts = defaultdict(int)

    order_rows = db.execute(
        select(Order.customer_id, func.count(Order.id), func.max(Order.created_at))
        .where(Order.company_id == company_id, Order.customer_id.in_(customer_ids))
        .group_by(Order.customer_id)
    ).all()
    validated_rows = db.execute(
        select(Order.validated_customer_id, func.count(Order.id), func.max(Order.created_at))
        .where(Order.company_id == company_id, Order.validated_customer_id.in_(customer_ids))
        .group_by(Order.validated_customer_id)
    ).all()
    for customer_id, count, last_at in order_rows + validated_rows:
        if not customer_id:
            continue
        ordered_counts[customer_id] += int(count or 0)
        if last_at and (customer_id not in latest or last_at > latest[customer_id]):
            latest[customer_id] = last_at

    contact_rows = db.execute(
        select(CustomerContact.customer_id, func.count(CustomerContact.id))
        .where(CustomerContact.company_id == company_id, CustomerContact.customer_id.in_(customer_ids))
        .group_by(CustomerContact.customer_id)
    ).all()
    for customer_id, count in contact_rows:
        line_counts[customer_id] = int(count or 0)
    return dict(ordered_counts), latest, dict(line_counts)


def _product_related_stats(db: Session, company_id: int, product_ids: list[int]) -> tuple[dict[int, int], dict[int, datetime | None]]:
    if not product_ids:
        return {}, {}
    counts = defaultdict(int)
    latest = {}

    row_sets = [
        db.execute(
            select(OrderLine.product_id, func.count(OrderLine.id), func.max(Order.created_at))
            .join(Order, Order.id == OrderLine.order_id)
            .where(Order.company_id == company_id, OrderLine.product_id.in_(product_ids))
            .group_by(OrderLine.product_id)
        ).all(),
        db.execute(
            select(OrderLine.validated_product_id, func.count(OrderLine.id), func.max(Order.created_at))
            .join(Order, Order.id == OrderLine.order_id)
            .where(Order.company_id == company_id, OrderLine.validated_product_id.in_(product_ids))
            .group_by(OrderLine.validated_product_id)
        ).all(),
    ]
    for rows in row_sets:
        for product_id, count, last_at in rows:
            if not product_id:
                continue
            counts[product_id] += int(count or 0)
            if last_at and (product_id not in latest or last_at > latest[product_id]):
                latest[product_id] = last_at
    return dict(counts), latest


def _customer_row(customer: Customer, orders_count: int, last_order_at: datetime | None, contact_count: int) -> dict:
    aliases = [alias.alias for alias in customer.aliases]
    domains = [domain.domain for domain in customer.domains]
    contacts = [contact.name for contact in getattr(customer, "contacts", []) if contact.name]
    contact_points = [point for point in getattr(customer, "contact_points", []) if point.active]
    email_points = [point.value for point in contact_points if point.type == "email"]
    phone_points = [point.value for point in contact_points if point.type in {"phone", "whatsapp"}]
    domain_points = [point.value for point in contact_points if point.type == "domain"]
    alias_points = [point.value for point in contact_points if point.type == "alias"]
    return {
        "id": customer.id,
        "code": customer.code,
        "fiscal_name": customer.fiscal_name,
        "commercial_name": customer.commercial_name or "",
        "tax_id": customer.tax_id or "",
        "primary_email": customer.primary_email or "",
        "delegation": customer.delegation or "",
        "phone": customer.phone or "",
        "address": customer.address or "",
        "city": customer.city or "",
        "province": customer.province or "",
        "country": customer.country or "",
        "assigned_salesperson": customer.assigned_salesperson or "",
        "accounting_code": customer.accounting_code or "",
        "category": customer.category or "",
        "notes": customer.notes or "",
        "status": customer.status or "active",
        "company_inactive": bool(customer.company_inactive),
        "aliases": aliases,
        "domains": domains,
        "contacts": contacts,
        "associated_emails": ", ".join(dict.fromkeys(email_points)),
        "associated_phones": ", ".join(dict.fromkeys(phone_points)),
        "associated_domains": ", ".join(dict.fromkeys(domain_points)),
        "associated_aliases": ", ".join(dict.fromkeys(alias_points)),
        "alias_count": len(aliases),
        "domain_count": len(domains),
        "contact_count": contact_count,
        "orders_count": orders_count,
        "last_order_at": _fmt_dt(last_order_at),
        "last_order_sort": last_order_at.timestamp() if last_order_at else 0,
        "status_label": "Eliminado" if getattr(customer, "deleted_at", None) else ("Inactivo" if customer.status != "active" or customer.company_inactive else "Activo"),
        "deleted_at": _fmt_dt(getattr(customer, "deleted_at", None)),
        "detail_aliases": ", ".join(aliases),
        "detail_domains": ", ".join(domains),
        "detail_contacts": ", ".join(contacts),
    }


def _product_row(product: Product, usage_count: int, last_used_at: datetime | None) -> dict:
    aliases = [alias.alias for alias in product.aliases]
    return {
        "id": product.id,
        "reference": product.reference,
        "alternative_code": product.alternative_code or "",
        "name": product.name,
        "brand": product.brand or "",
        "usual_supplier": product.usual_supplier or "",
        "description": product.description or "",
        "family": product.family or "",
        "subfamily": product.subfamily or "",
        "format": product.format or "",
        "sale_unit": product.sale_unit or "",
        "ean": product.ean or "",
        "sale_price": product.sale_price,
        "discount_percent": product.discount_percent,
        "size_group": product.size_group or "",
        "colors": product.colors or "",
        "entry_date": product.entry_date or "",
        "obsolete": bool(product.obsolete),
        "article_type": product.article_type or "",
        "description_cont": product.description_cont or "",
        "warehouse_location_code": product.warehouse_location_code or "",
        "replenishment_warehouse": product.replenishment_warehouse or "",
        "status": product.status or "active",
        "notes": product.notes or "",
        "aliases": aliases,
        "alias_count": len(aliases),
        "usage_count": usage_count,
        "last_used_at": _fmt_dt(last_used_at),
        "last_used_sort": last_used_at.timestamp() if last_used_at else 0,
        "status_label": "Eliminado" if getattr(product, "deleted_at", None) else ("Inactivo" if product.status != "active" or product.obsolete else "Activo"),
        "deleted_at": _fmt_dt(getattr(product, "deleted_at", None)),
        "detail_aliases": ", ".join(aliases),
    }


def customer_knowledge_overview(db: Session, company_id: int, customer_ids: list[int] | None = None) -> list[dict]:
    if customer_ids is not None and not customer_ids:
        return []

    customer_id_filter = None if customer_ids is None else CustomerProductKnowledge.customer_id.in_(customer_ids)
    knowledge_rows = db.execute(
        select(
            CustomerProductKnowledge.customer_id,
            func.count(CustomerProductKnowledge.id),
            func.sum(case((CustomerProductKnowledge.is_habitual.is_(True), 1), else_=0)),
            func.max(CustomerProductKnowledge.updated_at),
        )
        .where(CustomerProductKnowledge.company_id == company_id, *([customer_id_filter] if customer_id_filter is not None else []))
        .group_by(CustomerProductKnowledge.customer_id)
    ).all()
    knowledge_map = {customer_id: {"products": int(products or 0), "habitual": int(habitual or 0), "updated_at": updated_at} for customer_id, products, habitual, updated_at in knowledge_rows if customer_id}

    document_id_filter = None if customer_ids is None else RagDocument.source_entity_id.in_(customer_ids)
    doc_rows = db.execute(
        select(RagDocument.source_entity_id, func.count(RagDocument.id), func.max(RagDocument.created_at))
        .where(
            RagDocument.company_id == company_id,
            RagDocument.source_entity == "customer",
            *([document_id_filter] if document_id_filter is not None else []),
        )
        .group_by(RagDocument.source_entity_id)
    ).all()
    doc_map = {customer_id: {"documents": int(count or 0), "last_doc_at": updated_at} for customer_id, count, updated_at in doc_rows if customer_id}

    case_id_filter = None if customer_ids is None else RagCase.customer_id.in_(customer_ids)
    case_rows = db.execute(
        select(RagCase.customer_id, func.count(RagCase.id), func.max(RagCase.created_at))
        .where(RagCase.company_id == company_id, *([case_id_filter] if case_id_filter is not None else []))
        .group_by(RagCase.customer_id)
    ).all()
    case_map = {customer_id: {"cases": int(count or 0), "last_case_at": updated_at} for customer_id, count, updated_at in case_rows if customer_id}

    customers = db.scalars(
        select(Customer)
        .where(Customer.company_id == company_id, *([Customer.id.in_(customer_ids)] if customer_ids is not None else []))
        .options(selectinload(Customer.aliases), selectinload(Customer.domains), selectinload(Customer.contacts), selectinload(Customer.contact_points))
        .order_by(Customer.fiscal_name.asc())
    ).all()
    result: list[dict] = []
    for customer in customers:
        knowledge = knowledge_map.get(customer.id, {})
        docs = doc_map.get(customer.id, {})
        cases = case_map.get(customer.id, {})
        aliases = [alias.alias for alias in customer.aliases]
        domains = [domain.domain for domain in customer.domains]
        contacts = [contact for contact in customer.contacts if contact.name or contact.email or contact.phone]
        contact_points = [point for point in customer.contact_points if point.active]
        email_points = [point for point in contact_points if point.type == "email"]
        phone_points = [point for point in contact_points if point.type in {"phone", "whatsapp"}]
        domain_points = [point for point in contact_points if point.type == "domain"]
        total_docs = docs.get("documents", 0)
        total_products = knowledge.get("products", 0)
        total_habitual = knowledge.get("habitual", 0)
        has_notes = bool((customer.notes or "").strip())
        last_updated = max(
            [value for value in [knowledge.get("updated_at"), docs.get("last_doc_at"), cases.get("last_case_at")] if value],
            default=None,
        )
        if total_docs == 0 and total_products == 0 and not aliases and not contacts and not domains and not has_notes:
            status = "Sin conocimiento"
            state_key = "sin_conocimiento"
        elif total_docs == 0 and total_products == 0:
            status = "Pendiente de importar"
            state_key = "pendiente_importar"
        elif total_products >= 3 and total_docs >= 1:
            status = "Actualizado"
            state_key = "actualizado"
        elif total_products > 0 or total_docs > 0:
            status = "En construcción"
            state_key = "en_construccion"
        else:
            status = "Conocimiento parcial"
            state_key = "en_construccion"
        if cases.get("cases", 0) == 0 and total_products > 0:
            status = "Con sugerencias"
            state_key = "con_sugerencias"
        if any(alias for alias in aliases if "conflict" in alias.lower()):
            status = "Con errores"
            state_key = "con_errores"
        if total_docs == 0 and total_products == 0:
            quality_percent = 0
        else:
            quality_percent = min(
                100,
                (total_docs * 14)
                + (total_products * 4)
                + (total_habitual * 8)
                + (len(aliases) * 4)
                + (len(domains) * 3)
                + (len(contacts) * 3)
                + (len(contact_points) * 3)
                + (10 if has_notes else 0),
            )
        primary_endpoint = (
            (next((point.value for point in email_points if point.is_primary), None))
            or customer.primary_email
            or (domains[0] if domains else "")
            or (next((point.value for point in phone_points if point.is_primary), None))
            or (contacts[0].email if contacts and contacts[0].email else "")
            or (customer.phone or "")
        )
        habitual_channel = (
            "Email"
            if email_points or customer.primary_email
            else ("Dominio" if domain_points or domains else ("Contacto" if contacts else ("Teléfono" if phone_points or customer.phone else "Sin dato")))
        )
        result.append(
            {
                "id": customer.id,
                "code": customer.code,
                "name": customer.fiscal_name,
                "commercial_name": customer.commercial_name or "",
                "primary_email": next((point.value for point in email_points if point.is_primary), None) or customer.primary_email or "",
                "primary_domain": next((point.value for point in domain_points if point.is_primary), None) or (domains[0] if domains else ""),
                "primary_endpoint": primary_endpoint,
                "aliases": aliases,
                "domains": domains,
                "contact_points": [
                    {
                        "id": point.id,
                        "type": point.type,
                        "value": point.value,
                        "label": point.label or "",
                        "contact_name": point.contact_name or "",
                        "contact_role": point.contact_role or "",
                        "is_primary": point.is_primary,
                        "active": point.active,
                        "confidence": point.confidence,
                        "source": point.source,
                        "first_seen_at": _fmt_dt(point.first_seen_at),
                        "last_seen_at": _fmt_dt(point.last_seen_at),
                    }
                    for point in contact_points
                ],
                "emails_count": len(email_points) or (1 if customer.primary_email else 0),
                "phones_count": len(phone_points) or (1 if customer.phone else 0),
                "domains_count": len(domain_points) or len(domains),
                "contacts_count": len(contacts),
                "notes": customer.notes or "",
                "status_label": "Eliminado" if getattr(customer, "deleted_at", None) else ("Inactivo" if customer.status != "active" or customer.company_inactive else "Activo"),
                "status_key": state_key,
                "knowledge_state": status,
                "documents": total_docs,
                "habitual_products": total_habitual,
                "conditions": 1 if has_notes else 0,
                "comments": len(contacts),
                "suggestions": max(0, total_products - total_habitual),
                "last_updated": _fmt_dt(last_updated),
                "last_updated_sort": last_updated.timestamp() if last_updated else 0,
                "last_indexed": _fmt_dt(docs.get("last_doc_at")),
                "quality_percent": quality_percent,
                "habitual_channel": habitual_channel,
                "action_url": f"/customers?view=knowledge&selected_id={customer.id}",
                "secondary_url": f"/databases?tab=customers&selected_id={customer.id}",
                "knowledge_url": f"/customers/{customer.id}/knowledge",
                "list_url": f"/customers?view=list&selected_id={customer.id}",
                "import_url": f"/customers/{customer.id}/knowledge?section=importer",
                "upload_url": f"/customers/{customer.id}/knowledge?section=documents",
                "transform_url": f"/customers/{customer.id}/knowledge?section=transformer",
                "reindex_url": f"/customers/{customer.id}/knowledge?section=optimized",
                "learning_url": "/settings#knowledge",
            }
        )
    return result


def build_databases_context(
    db: Session,
    company_id: int,
    *,
    tab: str = "customers",
    q: str = "",
    status: str = "all",
    alias_mode: str = "all",
    family: str = "",
    selected_id: int = 0,
    sort: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    active_tab = tab if tab in {"customers", "products", "aliases", "imports", "knowledge"} else "customers"
    filters = {
        "tab": active_tab,
        "q": q,
        "status": status,
        "alias_mode": alias_mode,
        "family": family,
        "selected_id": selected_id,
        "sort": sort,
        "page": page,
        "page_size": page_size,
    }

    customer_rows: list[dict] = []
    product_rows: list[dict] = []
    alias_rows: list[dict] = []
    detail = None
    pagination = {
        "page": 1,
        "page_size": page_size,
        "total_items": 0,
        "total_pages": 0,
        "has_next": False,
        "has_previous": False,
        "start_item": 0,
        "end_item": 0,
        "allowed_page_sizes": (10, 25, 50, 100),
    }

    customers_total = db.scalar(select(func.count(Customer.id)).where(Customer.company_id == company_id, Customer.deleted_at.is_(None))) or 0
    products_total = db.scalar(select(func.count(Product.id)).where(Product.company_id == company_id, Product.deleted_at.is_(None))) or 0
    customer_alias_total = db.scalar(select(func.count(CustomerAlias.id)).where(CustomerAlias.company_id == company_id)) or 0
    product_alias_total = db.scalar(select(func.count(ProductAlias.id)).where(ProductAlias.company_id == company_id)) or 0
    active_customers = db.scalar(select(func.count(Customer.id)).where(Customer.company_id == company_id, Customer.deleted_at.is_(None), Customer.status == "active", Customer.company_inactive.is_(False))) or 0
    active_products = db.scalar(select(func.count(Product.id)).where(Product.company_id == company_id, Product.deleted_at.is_(None), Product.status == "active", Product.obsolete.is_(False))) or 0

    if active_tab == "customers":
        stmt = select(Customer).where(Customer.company_id == company_id, Customer.deleted_at.is_(None)).options(
            selectinload(Customer.aliases),
            selectinload(Customer.domains),
            selectinload(Customer.contacts),
            selectinload(Customer.contact_points),
        )
        if q:
            like = f"%{q}%"
            alias_exists = exists().where(CustomerAlias.company_id == company_id, CustomerAlias.customer_id == Customer.id, CustomerAlias.alias.ilike(like))
            domain_exists = exists().where(CustomerDomain.company_id == company_id, CustomerDomain.customer_id == Customer.id, CustomerDomain.domain.ilike(like))
            contact_point_exists = exists().where(
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.customer_id == Customer.id,
                or_(
                    CustomerContactPoint.value.ilike(like),
                    CustomerContactPoint.label.ilike(like),
                    CustomerContactPoint.contact_name.ilike(like),
                    CustomerContactPoint.contact_role.ilike(like),
                ),
            )
            contact_exists = exists().where(
                CustomerContact.company_id == company_id,
                CustomerContact.customer_id == Customer.id,
                or_(
                    CustomerContact.name.ilike(like),
                    CustomerContact.email.ilike(like),
                    CustomerContact.phone.ilike(like),
                ),
            )
            stmt = stmt.where(
                or_(
                    Customer.code.ilike(like),
                    Customer.fiscal_name.ilike(like),
                    Customer.commercial_name.ilike(like),
                    Customer.tax_id.ilike(like),
                    Customer.primary_email.ilike(like),
                    Customer.phone.ilike(like),
                    Customer.assigned_salesperson.ilike(like),
                    alias_exists,
                    domain_exists,
                    contact_point_exists,
                    contact_exists,
                )
            )
        if status == "active":
            stmt = stmt.where(Customer.status == "active", Customer.company_inactive.is_(False))
        elif status == "inactive":
            stmt = stmt.where(or_(Customer.status != "active", Customer.company_inactive.is_(True)))
        elif status == "with_alias":
            stmt = stmt.where(exists().where(CustomerAlias.company_id == company_id, CustomerAlias.customer_id == Customer.id))
        elif status == "without_alias":
            stmt = stmt.where(~exists().where(CustomerAlias.company_id == company_id, CustomerAlias.customer_id == Customer.id))
        elif status == "recent":
            stmt = stmt.where(Customer.created_at >= datetime.now(timezone.utc) - timedelta(days=30))
        customer_sort_map = {
            "name_asc": (Customer.company_inactive.asc(), Customer.fiscal_name.asc()),
            "name_desc": (Customer.company_inactive.asc(), Customer.fiscal_name.desc()),
            "code_asc": (Customer.code.asc()),
            "code_desc": (Customer.code.desc()),
            "email_asc": (Customer.primary_email.asc()),
            "email_desc": (Customer.primary_email.desc()),
            "city_asc": (Customer.city.asc()),
            "city_desc": (Customer.city.desc()),
            "status_asc": (Customer.status.asc()),
            "status_desc": (Customer.status.desc()),
            "tax_id_asc": (Customer.tax_id.asc()),
            "tax_id_desc": (Customer.tax_id.desc()),
            "phone_asc": (Customer.phone.asc()),
            "phone_desc": (Customer.phone.desc()),
            "channel_asc": (Customer.delegation.asc()),
            "channel_desc": (Customer.delegation.desc()),
            "address_asc": (Customer.address.asc()),
            "address_desc": (Customer.address.desc()),
            "notes_asc": (Customer.notes.asc()),
            "notes_desc": (Customer.notes.desc()),
            "last_activity_asc": (Customer.created_at.asc()),
            "last_activity_desc": (Customer.created_at.desc()),
            "created_asc": (Customer.created_at.asc()),
            "created_desc": (Customer.created_at.desc()),
        }
        order_clause = customer_sort_map.get(sort)
        if order_clause:
            if isinstance(order_clause, tuple):
                stmt = stmt.order_by(*order_clause)
            else:
                stmt = stmt.order_by(order_clause)
        else:
            stmt = stmt.order_by(Customer.company_inactive.asc(), Customer.fiscal_name.asc())
        customers, pagination = paginate(db, stmt, page=page, page_size=page_size)
        customer_ids = [customer.id for customer in customers]
        counts, latest, contact_counts = _customer_related_stats(db, company_id, customer_ids)
        customer_rows = [_customer_row(customer, counts.get(customer.id, 0), latest.get(customer.id), contact_counts.get(customer.id, 0)) for customer in customers]
        detail = customer_rows[0] if customer_rows else None
        if selected_id:
            for row in customer_rows:
                if row["id"] == selected_id:
                    detail = row
                    break
    elif active_tab == "products":
        stmt = select(Product).where(Product.company_id == company_id, Product.deleted_at.is_(None)).options(selectinload(Product.aliases))
        if q:
            like = f"%{q}%"
            alias_exists = exists().where(ProductAlias.company_id == company_id, ProductAlias.product_id == Product.id, ProductAlias.alias.ilike(like))
            stmt = stmt.where(
                or_(
                    Product.reference.ilike(like),
                    Product.alternative_code.ilike(like),
                    Product.name.ilike(like),
                    Product.description.ilike(like),
                    Product.family.ilike(like),
                    Product.subfamily.ilike(like),
                    Product.ean.ilike(like),
                    alias_exists,
                )
            )
        if family:
            stmt = stmt.where(Product.family == family)
        if status == "active":
            stmt = stmt.where(Product.status == "active", Product.obsolete.is_(False))
        elif status == "inactive":
            stmt = stmt.where(or_(Product.status != "active", Product.obsolete.is_(True)))
        elif status == "with_alias":
            stmt = stmt.where(exists().where(ProductAlias.company_id == company_id, ProductAlias.product_id == Product.id))
        elif status == "without_alias":
            stmt = stmt.where(~exists().where(ProductAlias.company_id == company_id, ProductAlias.product_id == Product.id))
        elif status == "recent":
            stmt = stmt.where(Product.created_at >= datetime.now(timezone.utc) - timedelta(days=30))
        product_sort_map = {
            "name_asc": Product.name.asc(),
            "name_desc": Product.name.desc(),
            "reference_asc": Product.reference.asc(),
            "reference_desc": Product.reference.desc(),
            "unit_asc": Product.sale_unit.asc(),
            "unit_desc": Product.sale_unit.desc(),
            "price_asc": Product.sale_price.asc(),
            "price_desc": Product.sale_price.desc(),
            "status_asc": Product.status.asc(),
            "status_desc": Product.status.desc(),
            "family_asc": Product.family.asc(),
            "family_desc": Product.family.desc(),
            "supplier_asc": Product.usual_supplier.asc(),
            "supplier_desc": Product.usual_supplier.desc(),
            "desc_asc": Product.description_cont.asc(),
            "desc_desc": Product.description_cont.desc(),
            "notes_asc": Product.notes.asc(),
            "notes_desc": Product.notes.desc(),
            "created_asc": Product.created_at.asc(),
            "created_desc": Product.created_at.desc(),
        }
        stmt = stmt.order_by(product_sort_map.get(sort, Product.reference.asc()))
        products, pagination = paginate(db, stmt, page=page, page_size=page_size)
        product_ids = [product.id for product in products]
        counts, latest = _product_related_stats(db, company_id, product_ids)
        product_rows = [_product_row(product, counts.get(product.id, 0), latest.get(product.id)) for product in products]
        detail = product_rows[0] if product_rows else None
        if selected_id:
            for row in product_rows:
                if row["id"] == selected_id:
                    detail = row
                    break
    elif active_tab == "aliases":
        customer_aliases = db.execute(
            select(CustomerAlias.alias, Customer.fiscal_name, Customer.code)
            .join(Customer, Customer.id == CustomerAlias.customer_id)
            .where(CustomerAlias.company_id == company_id, Customer.deleted_at.is_(None))
            .order_by(CustomerAlias.alias.asc())
        ).all()
        product_aliases = db.execute(
            select(ProductAlias.alias, Product.name, Product.reference)
            .join(Product, Product.id == ProductAlias.product_id)
            .where(ProductAlias.company_id == company_id, Product.deleted_at.is_(None))
            .order_by(ProductAlias.alias.asc())
        ).all()
        alias_rows = [
            {"kind": "customer", "alias": alias, "canonical": name, "reference": code}
            for alias, name, code in customer_aliases
        ] + [
            {"kind": "product", "alias": alias, "canonical": name, "reference": reference}
            for alias, name, reference in product_aliases
        ]
        alias_rows.sort(key=lambda item: (item["kind"], item["alias"].lower()))
        page, page_size = normalize_page(page, page_size)
        total_items = len(alias_rows)
        total_pages = (total_items + page_size - 1) // page_size if total_items else 0
        start = (page - 1) * page_size
        alias_rows = alias_rows[start:start + page_size]
        pagination = {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
            "start_item": start + 1 if total_items else 0,
            "end_item": min(start + page_size, total_items),
            "allowed_page_sizes": (25, 50, 100),
        }
    customer_knowledge = {"documents": [], "cases": [], "products": [], "summary": {"documents": 0, "cases": 0, "aliases": 0, "domains": 0, "contacts": 0, "products": 0, "habitual_products": 0}}
    if active_tab == "customers" and detail:
        customer_id = detail["id"]
        customer_doc_rows = db.scalars(
            select(RagDocument)
            .where(RagDocument.company_id == company_id, RagDocument.source_entity == "customer", RagDocument.source_entity_id == customer_id)
            .order_by(RagDocument.created_at.desc())
            .limit(6)
        ).all()
        customer_case_rows = db.scalars(
            select(RagCase)
            .where(RagCase.company_id == company_id, RagCase.customer_id == customer_id)
            .order_by(RagCase.created_at.desc())
            .limit(6)
        ).all()
        customer_product_rows = db.scalars(
            select(CustomerProductKnowledge)
            .where(CustomerProductKnowledge.company_id == company_id, CustomerProductKnowledge.customer_id == customer_id)
            .order_by(CustomerProductKnowledge.times_ordered.desc(), CustomerProductKnowledge.updated_at.desc())
            .limit(12)
        ).all()
        customer_knowledge = {
            "documents": [
                {
                    "id": doc.id,
                    "title": doc.title,
                    "source_type": doc.source_type,
                    "status": doc.embedding_status,
                    "content_excerpt": (doc.content_text or "")[:180],
                    "created_at": _fmt_dt(doc.created_at),
                }
                for doc in customer_doc_rows
            ],
            "cases": [
                {
                    "id": case.id,
                    "summary": case.summary,
                    "action": case.resolved_action,
                    "created_at": _fmt_dt(case.created_at),
                }
                for case in customer_case_rows
            ],
            "products": [
                {
                    "id": row.id,
                    "reference": row.product_reference,
                    "name": row.product_name,
                    "times_ordered": row.times_ordered,
                    "average_quantity": row.average_quantity,
                    "last_quantity": row.last_quantity,
                    "usual_unit": row.usual_unit or "",
                    "confidence": row.confidence,
                    "status": row.status,
                }
                for row in customer_product_rows
            ],
            "summary": {
                "documents": len(customer_doc_rows),
                "cases": len(customer_case_rows),
                "aliases": detail["alias_count"],
                "domains": detail["domain_count"],
                "contacts": detail["contact_count"],
                "products": len(customer_product_rows),
                "habitual_products": sum(1 for row in customer_product_rows if row.is_habitual),
            },
        }
    context = {
        "tab": active_tab,
        "filters": filters,
        "customers": customer_rows,
        "products": product_rows,
        "aliases": alias_rows,
        "detail": detail,
        "pagination": pagination,
        "stats": {
            "customers_total": customers_total,
            "products_total": products_total,
            "customer_alias_total": customer_alias_total,
            "product_alias_total": product_alias_total,
            "active_customers": active_customers,
            "active_products": active_products,
            "learned_aliases": db.scalar(select(func.count(LearnedAlias.id)).where(LearnedAlias.company_id == company_id)) or 0,
            "manual_corrections": db.scalar(select(func.count(ManualCorrection.id)).where(ManualCorrection.company_id == company_id)) or 0,
        },
        "customer_tabs": {
            "all": customers_total,
            "active": active_customers,
            "inactive": customers_total - active_customers,
            "with_alias": customer_alias_total,
        },
        "product_tabs": {
            "all": products_total,
            "active": active_products,
            "inactive": products_total - active_products,
            "with_alias": product_alias_total,
        },
        "customer_knowledge": customer_knowledge,
    }
    return context


def build_customer_context(
    db: Session,
    company_id: int,
    customer_id: int | None,
    *,
    order: Order | None = None,
    limit: int = 6,
) -> dict:
    if not customer_id:
        return {"identified": False}

    customer = db.scalar(
        select(Customer)
        .where(Customer.company_id == company_id, Customer.id == customer_id)
        .options(selectinload(Customer.aliases), selectinload(Customer.domains), selectinload(Customer.contacts), selectinload(Customer.contact_points))
    )
    if not customer:
        return {"identified": False}

    knowledge_rows = db.scalars(
        select(CustomerProductKnowledge)
        .where(CustomerProductKnowledge.company_id == company_id, CustomerProductKnowledge.customer_id == customer_id)
        .order_by(CustomerProductKnowledge.is_habitual.desc(), CustomerProductKnowledge.times_ordered.desc(), CustomerProductKnowledge.updated_at.desc())
        .limit(limit)
    ).all()
    doc_rows = db.scalars(
        select(RagDocument)
        .where(RagDocument.company_id == company_id, RagDocument.source_entity == "customer", RagDocument.source_entity_id == customer_id)
        .order_by(RagDocument.created_at.desc())
        .limit(limit)
    ).all()
    case_rows = db.scalars(
        select(RagCase)
        .where(RagCase.company_id == company_id, RagCase.customer_id == customer_id)
        .order_by(RagCase.created_at.desc())
        .limit(limit)
    ).all()
    correction_rows = db.scalars(
        select(ManualCorrection)
        .where(ManualCorrection.company_id == company_id, ManualCorrection.order_id == (order.id if order else None))
        .order_by(ManualCorrection.created_at.desc())
        .limit(limit)
    ).all() if order else []

    line_contexts: list[dict] = []
    order_lines = list(order.lines or []) if order else []
    for line in order_lines:
        product = line.validated_product or line.product
        knowledge = None
        if product:
            knowledge = next((row for row in knowledge_rows if row.product_id == product.id), None)
        line_contexts.append(
            {
                "line_id": line.id,
                "source_text": line.original_text or "",
                "product_label": f"{product.reference} · {product.name}" if product else (line.detected_product or "Producto sin identificar"),
                "matched": bool(knowledge),
                "times_ordered": knowledge.times_ordered if knowledge else 0,
                "usual_unit": knowledge.usual_unit if knowledge else "",
                "average_quantity": knowledge.average_quantity if knowledge else None,
                "last_quantity": knowledge.last_quantity if knowledge else None,
                "confidence": knowledge.confidence if knowledge else 0,
                "habitual": bool(knowledge.is_habitual) if knowledge else False,
                "comment": knowledge.comments_summary if knowledge else "",
                "anomaly": bool(
                    knowledge and line.quantity is not None and knowledge.average_quantity and line.quantity > (knowledge.average_quantity * 1.8)
                ),
            }
        )

    last_updated_dt = max(
        [value for value in [
            max((row.last_order_at for row in knowledge_rows if row.last_order_at), default=None),
            max((row.created_at for row in doc_rows if row.created_at), default=None),
            max((row.created_at for row in case_rows if row.created_at), default=None),
            max((row.created_at for row in correction_rows if row.created_at), default=None),
        ] if value],
        default=None,
    )
    last_indexed_dt = max((row.created_at for row in doc_rows if row.created_at), default=None)
    conflicts_pending = len([row for row in correction_rows if row.reason or row.original_value or row.corrected_value])

    return {
        "identified": True,
        "customer": {
            "id": customer.id,
            "code": customer.code,
            "name": customer.fiscal_name,
            "commercial_name": customer.commercial_name or "",
            "email": next((point.value for point in customer.contact_points if point.active and point.type == "email" and point.is_primary), None) or customer.primary_email or "",
            "phone": next((point.value for point in customer.contact_points if point.active and point.type in {"phone", "whatsapp"} and point.is_primary), None) or customer.phone or "",
            "delegation": customer.delegation or "",
            "status": customer.status or "active",
            "confidence": getattr(order, "customer_score", 0) if order else 0,
            "primary_endpoint": next((point.value for point in customer.contact_points if point.active and point.is_primary), None) or customer.primary_email or (customer.domains[0].domain if customer.domains else "") or (customer.contacts[0].email if customer.contacts and customer.contacts[0].email else "") or (customer.phone or ""),
            "habitual_channel": "Email" if any(point.active and point.type == "email" for point in customer.contact_points) or customer.primary_email else ("Dominio" if customer.domains else ("Contacto" if customer.contacts else ("Teléfono" if customer.phone else "Sin dato"))),
            "last_order_at": _fmt_dt(max([row.last_order_at for row in knowledge_rows if row.last_order_at], default=None)),
            "aliases": [alias.alias for alias in customer.aliases],
            "domains": [domain.domain for domain in customer.domains],
            "contacts": [contact.name or contact.email or "" for contact in customer.contacts],
            "associated_emails": ", ".join(
                dict.fromkeys(
                    [
                        point.value
                        for point in customer.contact_points
                        if point.active and point.type == "email" and point.value != (customer.primary_email or "")
                    ]
                )
            ),
            "associated_phones": ", ".join(
                dict.fromkeys(
                    [
                        point.value
                        for point in customer.contact_points
                        if point.active and point.type in {"phone", "whatsapp"} and point.value != (customer.phone or "")
                    ]
                )
            ),
            "associated_domains": ", ".join(
                dict.fromkeys(
                    [point.value for point in customer.contact_points if point.active and point.type == "domain"]
                )
            ),
            "associated_aliases": ", ".join(
                dict.fromkeys(
                    [point.value for point in customer.contact_points if point.active and point.type == "alias"]
                )
            ),
            "knowledge_url": f"/customers/{customer.id}/knowledge",
            "list_url": f"/customers?view=list&selected_id={customer.id}",
            "edit_url": f"/customers/{customer.id}/knowledge?section=summary",
            "import_url": f"/customers/{customer.id}/knowledge?section=import",
            "upload_url": f"/customers/{customer.id}/knowledge?section=import",
            "transform_url": f"/customers/{customer.id}/knowledge?section=import",
            "reindex_url": f"/customers/{customer.id}/knowledge?section=import",
            "orders_url": f"/pedidos?customer_id={customer.id}",
            "contact_points": [
                {
                    "id": point.id,
                    "type": point.type,
                    "value": point.value,
                    "label": point.label or "",
                    "contact_name": point.contact_name or "",
                    "contact_role": point.contact_role or "",
                    "is_primary": point.is_primary,
                    "active": point.active,
                    "confidence": point.confidence,
                    "source": point.source,
                    "first_seen_at": _fmt_dt(point.first_seen_at),
                    "last_seen_at": _fmt_dt(point.last_seen_at),
                }
                for point in customer.contact_points
            ],
            "notes": customer.notes or "",
        },
        "knowledge_rows": [
            {
                "id": row.id,
                "reference": row.product_reference,
                "product_name": row.product_name,
                "times_ordered": row.times_ordered,
                "last_order_at": row.last_order_at,
                "usual_unit": row.usual_unit or "",
                "average_quantity": row.average_quantity,
                "last_quantity": row.last_quantity,
                "confidence": row.confidence,
                "habitual": row.is_habitual,
                "status": row.status,
                "comments": row.comments_summary or "",
            }
            for row in knowledge_rows
        ],
        "documents": [
            {
                "id": doc.id,
                "title": doc.title,
                "status": doc.embedding_status,
                "excerpt": (doc.content_text or "")[:180],
            }
            for doc in doc_rows
        ],
        "cases": [
            {
                "id": case.id,
                "summary": case.summary,
                "action": case.resolved_action,
                "created_at": _fmt_dt(case.created_at),
            }
            for case in case_rows
        ],
        "corrections": [
            {
                "id": correction.id,
                "field": correction.field_name,
                "before": correction.original_value or "",
                "after": correction.corrected_value or "",
                "reason": correction.reason or "",
            }
            for correction in correction_rows
        ],
        "line_contexts": line_contexts,
        "summary": {
            "knowledge_rows": len(knowledge_rows),
            "documents": len(doc_rows),
            "cases": len(case_rows),
            "aliases": len(customer.aliases),
            "domains": len(customer.domains),
            "contacts": len(customer.contacts),
            "comments": len(customer.contacts),
            "contact_points": len(customer.contact_points),
            "emails": sum(1 for point in customer.contact_points if point.active and point.type == "email") or (1 if customer.primary_email else 0),
            "phones": sum(1 for point in customer.contact_points if point.active and point.type in {"phone", "whatsapp"}) or (1 if customer.phone else 0),
            "conditions": 1 if (customer.notes or "").strip() else 0,
            "last_updated": _fmt_dt(last_updated_dt),
            "last_indexed": _fmt_dt(last_indexed_dt),
            "conflicts_pending": conflicts_pending,
            "quality_percent": 0 if len(knowledge_rows) == 0 and len(doc_rows) == 0 else min(100, (len(knowledge_rows) * 14) + (len(doc_rows) * 14) + (len(case_rows) * 5) + (len(customer.aliases) * 4) + (len(customer.domains) * 3) + (len(customer.contacts) * 3) + (len(customer.contact_points) * 3) + (10 if (customer.notes or "").strip() else 0)),
        },
        "checklist": [
            {
                "title": "Revisar datos de identificación",
                "description": "Verifica código, razón social, CIF/NIF, email principal y dominios.",
                "status": "completed",
            },
            {
                "title": "Subir histórico de pedidos o albaranes",
                "description": "Importa documentos o pedidos previos para consolidar patrones.",
                "status": "completed" if doc_rows or knowledge_rows else "pending",
            },
            {
                "title": "Mapear columnas",
                "description": "Asocia referencias, cantidades, unidades y alias del cliente.",
                "status": "completed" if knowledge_rows else "pending",
            },
            {
                "title": "Ejecutar limpieza y agregación",
                "description": "Normaliza duplicados, comentarios y señales repetidas.",
                "status": "completed" if knowledge_rows and len(doc_rows) > 0 else "pending",
            },
            {
                "title": "Revisar sugerencias",
                "description": "Comprueba productos propuestos, alias aprendidos y advertencias.",
                "status": "completed" if case_rows or correction_rows else ("error" if not knowledge_rows and not doc_rows else "pending"),
            },
            {
                "title": "Reindexar conocimiento",
                "description": "Vuelve a generar embeddings y señales listas para el agente.",
                "status": "completed" if doc_rows else "pending",
            },
        ],
    }
