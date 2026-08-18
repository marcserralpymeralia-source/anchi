import logging

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.redirects import login_location_for_request
from app.master.database import get_master_db
from app.master.service import TenantUser, load_tenant_context

logger = logging.getLogger(__name__)


def _has_any_session_identity(session: dict) -> bool:
    return any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug"))


def _has_complete_session_identity(session: dict) -> bool:
    return all(session.get(key) for key in ("membership_id", "user_id", "company_id"))


def current_tenant_user(request: Request, master_db: Session = Depends(get_master_db)) -> TenantUser:
    tenant = getattr(request.state, "tenant", None)
    if tenant and tenant.user and tenant.user.is_active:
        return tenant.user
    session = request.scope.get("session") or {}

    try:
        tenant = load_tenant_context(request, master_db)
    except SQLAlchemyError as exc:
        logger.warning(
            "tenant_context_unavailable route=%s error_type=%s",
            request.url.path,
            exc.__class__.__name__,
            exc_info=True,
        )
        if _has_complete_session_identity(session):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tenant no disponible") from exc
        if _has_any_session_identity(session):
            request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)}) from exc
    if tenant and tenant.user and tenant.user.is_active:
        request.state.tenant = tenant
        return tenant.user

    if _has_complete_session_identity(session):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Membresia no disponible")
    if _has_any_session_identity(session):
        request.session.clear()
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})


def current_user(request: Request, master_db: Session = Depends(get_master_db)) -> TenantUser:
    return current_tenant_user(request, master_db)


def current_master_user(request: Request, master_db: Session = Depends(get_master_db)) -> TenantUser:
    return current_tenant_user(request, master_db)


def require_company_membership(company_id: int):
    def dependency(user: TenantUser = Depends(current_tenant_user)) -> TenantUser:
        if user.company_id != company_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La compañía no coincide con la membresía activa")
        return user

    return dependency


def require_tenant_role(*roles: str):
    allowed = {role for role in roles if role}

    def dependency(user: TenantUser = Depends(current_tenant_user)) -> TenantUser:
        if allowed and user.role.name not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")
        return user

    return dependency


def require_master_role(*roles: str):
    allowed = {role for role in roles if role} or {"Administrador", "Superadmin"}

    def dependency(user: TenantUser = Depends(current_master_user)) -> TenantUser:
        if user.role.name not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")
        return user

    return dependency


def require_master_admin(user: TenantUser = Depends(current_master_user)) -> TenantUser:
    if user.role.name != "Superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")
    return user


def current_company_id(user: TenantUser = Depends(current_tenant_user)) -> int:
    return user.company_id
