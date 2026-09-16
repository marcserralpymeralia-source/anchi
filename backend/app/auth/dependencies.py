import logging

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.redirects import login_location_for_request
from app.auth.sessions import validate_server_session
from app.core.permissions import has_permission, permission_for_request
from app.master.database import get_master_db
from app.master.models import MasterUser
from app.master.service import TenantUser, _platform_user_to_context, load_tenant_context

logger = logging.getLogger(__name__)


def _has_any_session_identity(session: dict) -> bool:
    return any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug"))


def _has_complete_session_identity(session: dict) -> bool:
    return all(session.get(key) for key in ("membership_id", "user_id", "company_id"))


def current_tenant_user(request: Request, master_db: Session = Depends(get_master_db)) -> TenantUser:
    tenant = getattr(request.state, "tenant", None)
    if tenant and tenant.user and tenant.user.is_active:
        required_permission = permission_for_request(request.method, request.url.path)
        if required_permission and not has_permission(tenant.user, required_permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tienes permisos para esta acción")
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
        required_permission = permission_for_request(request.method, request.url.path)
        if required_permission and not has_permission(tenant.user, required_permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tienes permisos para esta acción")
        return tenant.user

    if getattr(request.state, "auth_invalid", False):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})
    if _has_complete_session_identity(session):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Membresia no disponible")
    if _has_any_session_identity(session):
        request.session.clear()
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})


def current_user(request: Request, master_db: Session = Depends(get_master_db)) -> TenantUser:
    return current_tenant_user(request, master_db)


def current_master_user(request: Request, master_db: Session = Depends(get_master_db)) -> TenantUser:
    platform_user_id = (request.scope.get("session") or {}).get("platform_user_id")
    if platform_user_id:
        platform_user = master_db.get(MasterUser, platform_user_id)
        expected_version = (request.scope.get("session") or {}).get("platform_session_version")
        try:
            session_version_matches = (
                platform_user is not None
                and (expected_version is None or int(expected_version) == int(platform_user.session_version or 1))
            )
        except (TypeError, ValueError):
            session_version_matches = False
        if (
            platform_user
            and platform_user.is_active
            and platform_user.platform_role_key == "superadmin"
            and session_version_matches
            and validate_server_session(request, master_db, platform_user)
        ):
            return _platform_user_to_context(platform_user)
        if hasattr(request, "session"):
            request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})
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
    if user.platform_role_key != "superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")
    return user


def require_superadmin(user: TenantUser = Depends(current_master_user)) -> TenantUser:
    """Require a platform identity for the isolated Superadmin console."""

    if user.platform_role_key != "superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Solo el Superadmin de plataforma puede acceder aquí")
    return user


def current_company_id(user: TenantUser = Depends(current_tenant_user)) -> int:
    return user.company_id
