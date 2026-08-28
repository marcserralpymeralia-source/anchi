from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.db.models import Customer, Product
from app.master.service import TenantUser
from app.core.templating import templates
from app.tenancy.database import get_tenant_db
from app.databases.service import build_databases_context
from app.imports.service import create_preview
from app.master_data.service import upsert_customer, upsert_product
from app.jobs.service import enqueue_job
from app.logs.service import log_action

router = APIRouter(tags=["databases"])


@router.get("/databases")
def databases_page(
    request: Request,
    tab: str = "customers",
    q: str = "",
    status: str = "all",
    alias_mode: str = "all",
    family: str = "",
    selected_id: int = 0,
    sort: str = "",
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
        sort=sort,
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
    customer = upsert_customer(
        db,
        company_id=user.company_id,
        data={
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
            "address": address,
            "country": country,
            "notes": notes,
            "domains": domains,
            "aliases": aliases,
            "status": "inactive" if company_inactive else status,
        },
        source="manual",
        actor_id=user.id,
        customer_id=id or None,
        conflict_policy="update_existing",
    ).entity
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
    product = upsert_product(
        db,
        company_id=user.company_id,
        data={
            "reference": reference,
            "name": name,
            "brand": brand,
            "usual_supplier": usual_supplier,
            "alternative_code": alternative_code,
            "family": family,
            "subfamily": subfamily,
            "sale_price": "" if sale_price is None else str(sale_price),
            "discount_percent": "" if discount_percent is None else str(discount_percent),
            "size_group": size_group,
            "colors": colors,
            "entry_date": entry_date,
            "obsolete": "on" if obsolete else "",
            "article_type": article_type,
            "description_cont": description_cont,
            "warehouse_location_code": warehouse_location_code,
            "replenishment_warehouse": replenishment_warehouse,
            "sale_unit": sale_unit,
            "ean": ean,
            "notes": notes,
            "aliases": aliases,
            "status": "inactive" if obsolete else status,
        },
        source="manual",
        actor_id=user.id,
        product_id=id or None,
        conflict_policy="update_existing",
    ).entity
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
