from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_superadmin
from app.auth.sessions import rotate_server_session
from app.core.templating import templates
from app.master.database import get_master_db
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, PlatformAuditLog
from app.master.provisioning import find_tenant_actor_id
from app.master.service import TenantUser
from app.superadmin.metrics import latest_health_snapshots
from app.superadmin.service import TENANT_ROLES, create_company, create_company_user, platform_stats, retry_company_provisioning, toggle_company, toggle_user


router = APIRouter(prefix="/superadmin", tags=["superadmin"])


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    return RedirectResponse(f"{path}?error={quote(message)}" if message else path, status_code=303)


def _base_context(request: Request, user: TenantUser, **extra):
    return {"request": request, "user": user, **extra}


def _page_params(request: Request) -> tuple[int, int]:
    try:
        page = max(int(request.query_params.get("page", "1") or 1), 1)
    except ValueError:
        page = 1
    try:
        page_size = min(max(int(request.query_params.get("page_size", "50") or 50), 10), 100)
    except ValueError:
        page_size = 50
    return page, page_size


def _companies(master_db: Session, *, offset: int = 0, limit: int | None = None) -> list[dict]:
    company_query = select(MasterCompany).order_by(MasterCompany.name).offset(max(offset, 0))
    if limit is not None:
        company_query = company_query.limit(max(limit, 1))
    companies = master_db.scalars(company_query).all()
    company_ids = [company.id for company in companies]
    membership_counts = dict(master_db.execute(select(CompanyMembership.company_id, func.count(CompanyMembership.id)).where(CompanyMembership.company_id.in_(company_ids)).group_by(CompanyMembership.company_id)).all()) if company_ids else {}
    db_rows = {row.company_id: row for row in master_db.execute(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id.in_(company_ids))).scalars().all()} if company_ids else {}
    health_rows = {row.company_id: row for row in latest_health_snapshots(master_db, company_ids)}
    result = []
    for company in companies:
        tenant_db = db_rows.get(company.id)
        result.append({"company": company, "memberships": int(membership_counts.get(company.id, 0)), "tenant_db": tenant_db, "health": health_rows.get(company.id)})
    return result


@router.get("")
def dashboard(request: Request, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    recent_audit = master_db.scalars(select(PlatformAuditLog).order_by(PlatformAuditLog.created_at.desc()).limit(8)).all()
    return templates.TemplateResponse("superadmin/dashboard.html", _base_context(request, user, title="Resumen", stats=platform_stats(master_db), companies=_companies(master_db)[:6], recent_audit=recent_audit))


@router.get("/companies")
def companies_page(request: Request, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    page, page_size = _page_params(request)
    company_total = int(master_db.scalar(select(func.count(MasterCompany.id))) or 0)
    total_pages = (company_total + page_size - 1) // page_size if company_total else 1
    page = min(page, total_pages)
    return templates.TemplateResponse("superadmin/companies.html", _base_context(request, user, title="Empresas", companies=_companies(master_db, offset=(page - 1) * page_size, limit=page_size), company_total=company_total, page=page, total_pages=total_pages, error=request.query_params.get("error")))


@router.post("/companies")
def companies_create(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    database_url: str = Form(""),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_superadmin),
):
    try:
        create_company(master_db, name=name, slug=slug, database_url=database_url, actor_user_id=user.id, provision_async=True)
    except ValueError as exc:
        return _redirect("/superadmin/companies", str(exc))
    return _redirect("/superadmin/companies")


@router.post("/companies/{company_id}/toggle")
def companies_toggle(company_id: int, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    try:
        toggle_company(master_db, company_id, user.id)
    except ValueError as exc:
        return _redirect("/superadmin/companies", str(exc))
    return _redirect("/superadmin/companies")


@router.post("/companies/{company_id}/retry-provisioning")
def companies_retry_provisioning(company_id: int, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    try:
        retry_company_provisioning(master_db, company_id, user.id)
    except ValueError as exc:
        return _redirect("/superadmin/companies", str(exc))
    return _redirect("/superadmin/companies")


@router.post("/companies/{company_id}/enter")
def enter_company(
    request: Request,
    company_id: int,
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_superadmin),
):
    """Switch a platform user into one of their explicit company memberships."""

    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company_id,
            CompanyMembership.is_active.is_(True),
        )
    )
    company = master_db.get(MasterCompany, company_id)
    if membership is None and company is not None and company.active:
        membership = CompanyMembership(user_id=user.id, company_id=company_id, role_key="Solo lectura", is_active=True, is_owner=False)
        master_db.add(membership)
        master_db.commit()
    if not membership or not company or not company.active:
        return _redirect("/superadmin/companies", "No tienes una membresía activa para esa empresa")
    tenant_db = master_db.scalar(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == company_id,
            MasterTenantDatabase.is_active.is_(True),
        )
    )
    if not tenant_db or not tenant_db.database_url:
        return _redirect("/superadmin/companies", "La empresa todavía no tiene una base operativa disponible")

    request.session["user_id"] = user.id
    request.session["platform_user_id"] = user.id
    request.session["platform_session_version"] = user.session_version
    request.session["company_id"] = company.id
    request.session["membership_id"] = membership.id
    request.session["company_slug"] = company.slug
    request.session["tenant_session_version"] = user.session_version
    request.session.pop("tenant_actor_id", None)
    actor_id = find_tenant_actor_id(tenant_db.database_url, company_id=company.id, master_user_id=user.id)
    if actor_id is not None:
        request.session["tenant_actor_id"] = actor_id
    else:
        return _redirect("/superadmin/companies", "La identidad local todavía no está sincronizada; reintenta tras completar el aprovisionamiento")
    master_user = master_db.get(MasterUser, user.id)
    if master_user is not None:
        rotate_server_session(
            master_db,
            request,
            master_user,
            company_id=company.id,
            membership_id=membership.id,
        )
    return RedirectResponse("/", status_code=303)


@router.get("/users")
def users_page(request: Request, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    companies = master_db.scalars(
        select(MasterCompany)
        .where(MasterCompany.active.is_(True), MasterCompany.status == "active")
        .order_by(MasterCompany.name)
    ).all()
    page, page_size = _page_params(request)
    user_total = int(master_db.scalar(select(func.count(MasterUser.id))) or 0)
    total_pages = (user_total + page_size - 1) // page_size if user_total else 1
    page = min(page, total_pages)
    page_users = master_db.scalars(select(MasterUser).order_by(MasterUser.full_name, MasterUser.email).offset((page - 1) * page_size).limit(page_size)).all()
    user_ids = [item.id for item in page_users]
    memberships = master_db.scalars(select(CompanyMembership).where(CompanyMembership.user_id.in_(user_ids)).order_by(CompanyMembership.company_id, CompanyMembership.user_id)).all() if user_ids else []
    memberships_by_user: dict[int, list[CompanyMembership]] = {}
    for membership in memberships:
        memberships_by_user.setdefault(membership.user_id, []).append(membership)
    rows = [{"user": item, "memberships": memberships_by_user.get(item.id, [])} for item in page_users]
    return templates.TemplateResponse("superadmin/users.html", _base_context(request, user, title="Usuarios", companies=companies, rows=rows, user_total=user_total, page=page, total_pages=total_pages, roles=TENANT_ROLES, error=request.query_params.get("error")))


@router.post("/users")
def users_create(
    company_id: int = Form(...),
    full_name: str = Form(""),
    email: str = Form(...),
    password: str = Form(""),
    role_key: str = Form("Operador"),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_superadmin),
):
    try:
        create_company_user(master_db, company_id=company_id, full_name=full_name, email=email, password=password or None, role_key=role_key, actor_user_id=user.id)
    except ValueError as exc:
        return _redirect("/superadmin/users", str(exc))
    return _redirect("/superadmin/users")


@router.post("/users/{user_id}/toggle")
def users_toggle(user_id: int, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    try:
        toggle_user(master_db, user_id, user.id)
    except ValueError as exc:
        return _redirect("/superadmin/users", str(exc))
    return _redirect("/superadmin/users")


@router.get("/audit")
def audit_page(request: Request, master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_superadmin)):
    events = master_db.scalars(select(PlatformAuditLog).order_by(PlatformAuditLog.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse("superadmin/audit.html", _base_context(request, user, title="Auditoría", events=events))


@router.get("/api/stats")
def stats_api(master_db: Session = Depends(get_master_db), _: TenantUser = Depends(require_superadmin)):
    return JSONResponse(platform_stats(master_db))


@router.get("/api/health")
def health_api(master_db: Session = Depends(get_master_db), _: TenantUser = Depends(require_superadmin)):
    return JSONResponse(
        {
            "items": [
                {
                    "company_id": row.company_id,
                    "status": row.status,
                    "checked_at": row.checked_at.isoformat() if row.checked_at else None,
                    "latency_ms": row.latency_ms,
                    "orders_total": row.orders_total,
                    "messages_total": row.messages_total,
                    "pending_jobs_total": row.pending_jobs_total,
                    "error_code": row.error_code,
                }
                for row in latest_health_snapshots(master_db)
            ]
        }
    )
