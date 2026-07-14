from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from datetime import datetime, timezone

from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.templating import templates
from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.pagination import paginate
from app.tenancy.database import get_tenant_db
from app.db.models import CustomerProductKnowledge, Order, OrderLine, Product, ProductAlias, User
from app.imports.service import import_products, read_table, read_table_from_bytes
from app.logs.service import log_action

router = APIRouter(prefix="/products", tags=["products"])


_DELETE_ROLES = {"Administrador", "Superadmin", "Owner", "Propietario"}
_FINAL_ORDER_STATUSES = {"pedido_confirmado", "pedido_exportado", "no_pedido", "descartado", "deleted", "archived_deleted"}


def _can_delete(user: TenantUser) -> bool:
    return user.role.name in _DELETE_ROLES


def _product_has_open_orders(db: Session, company_id: int, product_id: int) -> bool:
    return bool(
        db.scalar(
            select(Order.id)
            .join(OrderLine, OrderLine.order_id == Order.id)
            .where(
                Order.company_id == company_id,
                Order.deleted_at.is_(None),
                Order.status.notin_(_FINAL_ORDER_STATUSES),
                or_(OrderLine.product_id == product_id, OrderLine.validated_product_id == product_id),
            )
            .limit(1)
        )
    )


def _soft_delete_product(db: Session, product: Product, user: TenantUser) -> None:
    product.status = "deleted"
    product.obsolete = True
    product.deleted_at = datetime.now(timezone.utc)
    product.deleted_by = user.id
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="product.delete", entity_type="product", entity_id=product.id, message="Producto eliminado")


@router.get("")
def list_products(
    request: Request,
    q: str = "",
    family: str = "",
    obsolete: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    stmt = select(Product).where(Product.company_id == user.company_id).options(selectinload(Product.aliases))
    if q:
        like = f"%{q}%"
        alias_exists = exists().where(ProductAlias.company_id == user.company_id, ProductAlias.product_id == Product.id, ProductAlias.alias.ilike(like))
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
    if obsolete == "yes":
        stmt = stmt.where(Product.obsolete.is_(True))
    elif obsolete == "no":
        stmt = stmt.where(Product.obsolete.is_(False))
    stmt = stmt.order_by(Product.reference.asc())
    products, pagination = paginate(db, stmt, page=page, page_size=page_size)
    families = db.scalars(select(Product.family).where(Product.company_id == user.company_id, Product.family.is_not(None)).distinct().order_by(Product.family)).all()
    product_ids = [product.id for product in products]
    relationship_rows = db.execute(
        select(
            CustomerProductKnowledge.product_id,
            func.count(CustomerProductKnowledge.id),
            func.count(func.distinct(CustomerProductKnowledge.customer_id)),
            func.max(CustomerProductKnowledge.updated_at),
        )
        .where(CustomerProductKnowledge.company_id == user.company_id, CustomerProductKnowledge.product_id.in_(product_ids))
        .group_by(CustomerProductKnowledge.product_id)
    ).all()
    product_stats = {
        product_id: {
            "relationships": int(relations or 0),
            "customers": int(customers or 0),
            "last_import": last_updated.strftime("%d/%m/%Y %H:%M") if last_updated else "",
            "last_import_sort": last_updated.timestamp() if last_updated else 0,
        }
        for product_id, relations, customers, last_updated in relationship_rows
        if product_id
    }
    return templates.TemplateResponse(
        "products/list.html",
        {
            "request": request,
            "user": user,
            "title": "Productos",
            "products": products,
            "families": families,
            "pagination": pagination,
            "q": q,
            "family": family,
            "obsolete": obsolete,
            "product_stats": product_stats,
        },
    )


@router.post("")
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
    if product.company_id == user.company_id and product.deleted_at is None:
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
        product.status = "inactive" if obsolete else status
        db.query(ProductAlias).filter(ProductAlias.product_id == product.id).delete()
        for alias in [a.strip() for a in aliases.split(",") if a.strip()]:
            db.add(ProductAlias(company_id=user.company_id, product_id=product.id, alias=alias))
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="product.save", entity_type="product", entity_id=product.id, message=f"Producto guardado: {reference}")
    return RedirectResponse("/products", status_code=303)


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
        filename = file.filename or "productos"
    elif pasted_text.strip():
        filename = "productos.csv"
        df = read_table_from_bytes(pasted_text.encode(encoding), filename, encoding=encoding)
    else:
        raise HTTPException(status_code=400, detail="Debes adjuntar un archivo o pegar una tabla para importar productos.")
    job = import_products(db, company_id=user.company_id, filename=filename, df=df)
    log_action(db, company_id=user.company_id, user=user, action="products.import", entity_type="import", entity_id=job.id, message=f"Importados productos: {job.rows_created} creados, {job.rows_updated} actualizados")
    return RedirectResponse("/products", status_code=303)


@router.get("/import")
def import_file_legacy() -> RedirectResponse:
    return RedirectResponse("/products", status_code=303)


@router.post("/{product_id}/delete")
def delete_product_post(product_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return delete_product(product_id, request, db, user)


@router.delete("/{product_id}")
def delete_product(product_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_delete(user):
        raise HTTPException(status_code=403, detail="Sin permisos para eliminar.")
    product = db.get(Product, product_id)
    if not product or product.company_id != user.company_id or product.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")
    if _product_has_open_orders(db, user.company_id, product_id):
        raise HTTPException(status_code=400, detail="Este producto está en pedidos activos. Cierra o reasigna esos pedidos antes de eliminarlo.")
    _soft_delete_product(db, product, user)
    return RedirectResponse(request.headers.get("referer") or "/products", status_code=303)


@router.post("/bulk-delete")
def bulk_delete_products(ids: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_delete(user):
        raise HTTPException(status_code=403, detail="Sin permisos para eliminar.")
    raw_ids = [int(item) for item in ids.split(",") if item.strip().isdigit()]
    deleted = 0
    blocked = 0
    for product in db.scalars(select(Product).where(Product.company_id == user.company_id, Product.id.in_(raw_ids), Product.deleted_at.is_(None))).all():
        if _product_has_open_orders(db, user.company_id, product.id):
            blocked += 1
            continue
        _soft_delete_product(db, product, user)
        deleted += 1
    return JSONResponse({"success": True, "deleted": deleted, "blocked": blocked, "message": "Productos eliminados correctamente"})
