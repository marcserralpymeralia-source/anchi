from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.templating import templates
from app.tenancy.database import get_tenant_db
from app.db.models import Customer, CustomerAlias, CustomerContactPoint, CustomerDomain, Product, ProductAlias, User
from app.databases.service import build_databases_context
from app.imports.service import create_preview
from app.jobs.service import enqueue_job
from app.logs.service import log_action

router = APIRouter(tags=["databases"])


def _sync_contact_points(
    db: Session,
    *,
    customer: Customer,
    associated_emails: str = "",
    associated_phones: str = "",
    domains: str = "",
    aliases: str = "",
) -> None:
    db.query(CustomerContactPoint).filter(CustomerContactPoint.customer_id == customer.id).delete()
    now = datetime.now(timezone.utc)
    def _items(raw: str) -> list[str]:
        return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    if customer.primary_email:
        db.add(CustomerContactPoint(company_id=customer.company_id, customer_id=customer.id, type="email", value=customer.primary_email.strip().lower(), label="principal", is_primary=True, active=True, confidence=0.9, source="manual", first_seen_at=now, last_seen_at=now))
    if customer.phone:
        db.add(CustomerContactPoint(company_id=customer.company_id, customer_id=customer.id, type="phone", value=customer.phone.strip(), label="principal", is_primary=True, active=True, confidence=0.8, source="manual", first_seen_at=now, last_seen_at=now))
    for email in _items(associated_emails):
        db.add(CustomerContactPoint(company_id=customer.company_id, customer_id=customer.id, type="email", value=email.lower(), label="asociado", is_primary=False, active=True, confidence=0.7, source="manual", first_seen_at=now, last_seen_at=now))
    for phone in _items(associated_phones):
        db.add(CustomerContactPoint(company_id=customer.company_id, customer_id=customer.id, type="phone", value=phone, label="asociado", is_primary=False, active=True, confidence=0.7, source="manual", first_seen_at=now, last_seen_at=now))
    for domain in _items(domains):
        db.add(CustomerContactPoint(company_id=customer.company_id, customer_id=customer.id, type="domain", value=domain.lower(), label="dominio", is_primary=False, active=True, confidence=0.8, source="manual", first_seen_at=now, last_seen_at=now))
    for alias in _items(aliases):
        db.add(CustomerContactPoint(company_id=customer.company_id, customer_id=customer.id, type="alias", value=alias.lower(), label="alias", is_primary=False, active=True, confidence=0.7, source="manual", first_seen_at=now, last_seen_at=now))


@router.get("/databases")
def databases_page(
    request: Request,
    tab: str = "customers",
    q: str = "",
    status: str = "all",
    alias_mode: str = "all",
    family: str = "",
    selected_id: int = 0,
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    context = build_databases_context(
        db,
        user.company_id,
        tab=tab,
        q=q,
        status=status,
        alias_mode=alias_mode,
        family=family,
        selected_id=selected_id,
        page=page,
        page_size=page_size,
    )
    families = db.scalars(select(Product.family).where(Product.company_id == user.company_id, Product.family.is_not(None)).distinct().order_by(Product.family)).all()
    categories = db.scalars(select(Customer.category).where(Customer.company_id == user.company_id, Customer.category.is_not(None)).distinct().order_by(Customer.category)).all()
    return templates.TemplateResponse(
        "databases/list.html",
        {
            "request": request,
            "user": user,
            "families": families,
            "categories": categories,
            **context,
        },
    )


@router.get("/databases/customers")
def customers_redirect():
    return RedirectResponse("/databases?tab=customers", status_code=303)


@router.get("/databases/products")
def products_redirect():
    return RedirectResponse("/databases?tab=products", status_code=303)


@router.post("/customers")
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
    address: str = Form(""),
    country: str = Form(""),
    notes: str = Form(""),
    domains: str = Form(""),
    aliases: str = Form(""),
    status: str = Form("active"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    customer = db.get(Customer, id) if id else None
    if not customer:
        customer = Customer(company_id=user.company_id, code=code, fiscal_name=fiscal_name)
        db.add(customer)
        db.flush()
    if customer.company_id == user.company_id:
        customer.code = code
        customer.fiscal_name = fiscal_name
        customer.tax_id = tax_id
        customer.delegation = delegation
        customer.phone = phone
        customer.city = city
        customer.province = province
        customer.assigned_salesperson = assigned_salesperson
        customer.accounting_code = accounting_code
        customer.company_inactive = company_inactive
        customer.category = category
        customer.commercial_name = commercial_name
        customer.primary_email = primary_email
        customer.address = address
        customer.country = country
        customer.notes = notes
        customer.status = "inactive" if company_inactive else status
        db.query(CustomerAlias).filter(CustomerAlias.customer_id == customer.id).delete()
        db.query(CustomerDomain).filter(CustomerDomain.customer_id == customer.id).delete()
        for alias in [a.strip() for a in aliases.split(",") if a.strip()]:
            db.add(CustomerAlias(company_id=user.company_id, customer_id=customer.id, alias=alias))
        for domain in [d.strip().lower() for d in domains.split(",") if d.strip()]:
            db.add(CustomerDomain(company_id=user.company_id, customer_id=customer.id, domain=domain))
        _sync_contact_points(
            db,
            customer=customer,
            associated_emails=associated_emails,
            associated_phones=associated_phones,
            domains=domains,
            aliases=aliases,
        )
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="database.customer.save", entity_type="customer", entity_id=customer.id, message=f"Cliente guardado: {code}")
    return RedirectResponse("/databases?tab=customers", status_code=303)


@router.post("/products")
def save_product(
    id: int = Form(0),
    reference: str = Form(...),
    name: str = Form(...),
    brand: str = Form(""),
    usual_supplier: str = Form(""),
    alternative_code: str = Form(""),
    family: str = Form(""),
    subfamily: str = Form(""),
    sale_price: float | None = Form(None),
    discount_percent: float | None = Form(None),
    size_group: str = Form(""),
    colors: str = Form(""),
    entry_date: str = Form(""),
    obsolete: bool = Form(False),
    article_type: str = Form(""),
    description_cont: str = Form(""),
    warehouse_location_code: str = Form(""),
    replenishment_warehouse: str = Form(""),
    sale_unit: str = Form(""),
    ean: str = Form(""),
    notes: str = Form(""),
    aliases: str = Form(""),
    status: str = Form("active"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    product = db.get(Product, id) if id else None
    if not product:
        product = Product(company_id=user.company_id, reference=reference, name=name)
        db.add(product)
        db.flush()
    if product.company_id == user.company_id:
        product.reference = reference
        product.name = name
        product.description = name
        product.brand = brand
        product.usual_supplier = usual_supplier
        product.alternative_code = alternative_code
        product.family = family
        product.subfamily = subfamily
        product.sale_price = sale_price
        product.discount_percent = discount_percent
        product.size_group = size_group
        product.colors = colors
        product.entry_date = entry_date
        product.obsolete = obsolete
        product.article_type = article_type
        product.description_cont = description_cont
        product.warehouse_location_code = warehouse_location_code
        product.replenishment_warehouse = replenishment_warehouse
        product.sale_unit = sale_unit
        product.ean = ean
        product.notes = notes
        product.status = "inactive" if obsolete else status
        db.query(ProductAlias).filter(ProductAlias.product_id == product.id).delete()
        for alias in [a.strip() for a in aliases.split(",") if a.strip()]:
            db.add(ProductAlias(company_id=user.company_id, product_id=product.id, alias=alias))
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="database.product.save", entity_type="product", entity_id=product.id, message=f"Producto guardado: {reference}")
    return RedirectResponse("/databases?tab=products", status_code=303)


@router.post("/databases/import/customers")
async def import_customers_file(file: UploadFile = File(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    preview = await create_preview(file, "customers")
    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="import_file",
        payload={"token": preview["token"], "filename": preview["filename"], "entity_type": "customers", "encoding": "utf-8"},
        created_by_user_id=user.id,
    )
    log_action(db, company_id=user.company_id, user=user, action="database.customers.import", entity_type="job", entity_id=job.id, message="Importacion de clientes encolada")
    return RedirectResponse("/imports/history", status_code=303)


@router.post("/databases/import/products")
async def import_products_file(file: UploadFile = File(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    preview = await create_preview(file, "products")
    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="import_file",
        payload={"token": preview["token"], "filename": preview["filename"], "entity_type": "products", "encoding": "utf-8"},
        created_by_user_id=user.id,
    )
    log_action(db, company_id=user.company_id, user=user, action="database.products.import", entity_type="job", entity_id=job.id, message="Importacion de productos encolada")
    return RedirectResponse("/imports/history", status_code=303)
