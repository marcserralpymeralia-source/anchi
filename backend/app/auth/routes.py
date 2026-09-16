import logging
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.redirects import DEFAULT_LOGIN_DESTINATION, safe_internal_next
from app.auth.rate_limit import consume_authentication, consume_public_action, reset_authentication
from app.auth.service import authenticate_user
from app.auth.sessions import SESSION_KEY, bind_server_session_context, create_server_session, revoke_server_session, rotate_server_session
from app.auth.lifecycle import accept_invitation, create_password_reset_token, reset_password, send_password_reset_email
from app.core.templating import templates
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.core.config import get_settings
from app.master.database import get_master_db
from app.master.provisioning import find_tenant_actor_id, synchronize_tenant_actors
from app.master.service import is_configured_platform_owner
from app.setup.service import get_setup_status
from app.tenancy.database import tenant_db_session
from app.settings.branding import branding_to_dict, default_branding_payload

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_tenant_actor_id(
    master_db: Session,
    *,
    database_url: str,
    company_id: int,
    master_user_id: int,
    email: str,
) -> int | None:
    """Resolve an actor and repair legacy/missing projections once if needed."""

    actor_id = find_tenant_actor_id(
        database_url,
        company_id=company_id,
        master_user_id=master_user_id,
        email=email,
    )
    if actor_id is not None:
        return actor_id

    tenant_row = master_db.scalar(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == company_id,
            MasterTenantDatabase.is_active.is_(True),
        )
    )
    if tenant_row is None or tenant_row.database_url != database_url:
        return None
    try:
        synchronize_tenant_actors(master_db, tenant_row)
    except Exception:  # noqa: BLE001
        logger.warning(
            "No se pudo reparar la identidad local company_id=%s master_user_id=%s",
            company_id,
            master_user_id,
            exc_info=True,
        )
        return None
    return find_tenant_actor_id(
        database_url,
        company_id=company_id,
        master_user_id=master_user_id,
        email=email,
    )


@router.get("/login")
def login_page(request: Request, next: str = ""):
    next_url = safe_internal_next(next)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "message": request.query_params.get("message"),
            "next_url": next_url,
            "login_branding": branding_to_dict(default_branding_payload()),
        },
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(DEFAULT_LOGIN_DESTINATION),
    master_db: Session = Depends(get_master_db),
):
    next_url = safe_internal_next(next)
    rate_decision = consume_authentication(master_db, request, email)
    if not rate_decision.allowed:
        response = templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Demasiados intentos. Espera unos minutos e inténtalo de nuevo.",
                "message": None,
                "next_url": next_url,
                "login_branding": branding_to_dict(default_branding_payload()),
            },
            status_code=429,
        )
        response.headers["Retry-After"] = str(rate_decision.retry_after)
        return response
    user = authenticate_user(master_db, email, password)

    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Credenciales no validas",
                "message": None,
                "next_url": next_url,
                "login_branding": branding_to_dict(default_branding_payload()),
            },
            status_code=401,
        )

    request.session.clear()
    master_user = master_db.get(MasterUser, user.id)
    if master_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "La cuenta no está disponible",
                "message": None,
                "next_url": next_url,
                "login_branding": branding_to_dict(default_branding_payload()),
            },
            status_code=401,
        )

    reset_authentication(master_db, request, email)
    server_session_id = create_server_session(master_db, request, master_user)
    request.session[SESSION_KEY] = server_session_id
    request.session["user_id"] = user.id
    if is_configured_platform_owner(master_user):
        request.session["platform_user_id"] = user.id
        request.session["platform_session_version"] = getattr(user, "session_version", 1)
        next_url = "/superadmin"
    else:
        memberships = []
        if hasattr(master_db, "scalars"):
            memberships = master_db.scalars(
                select(CompanyMembership)
                .options(selectinload(CompanyMembership.company))
                .where(
                    CompanyMembership.user_id == user.id,
                    CompanyMembership.is_active.is_(True),
                    MasterCompany.active.is_(True),
                    MasterCompany.status == "active",
                )
                .join(MasterCompany, MasterCompany.id == CompanyMembership.company_id)
                .order_by(CompanyMembership.is_owner.desc(), CompanyMembership.id.asc())
            ).all()
        if getattr(user, "platform_role_key", None) != "superadmin" and len(memberships) > 1:
            request.session["pending_company_selection"] = True
            request.session["tenant_session_version"] = getattr(user, "session_version", 1)
            request.session["login_next"] = next_url
            return RedirectResponse("/select-company", status_code=303)
        request.session["company_id"] = user.company_id
        request.session["membership_id"] = user.membership_id
        request.session["company_slug"] = user.company_slug
        request.session["tenant_session_version"] = getattr(user, "session_version", 1)
        request.session.pop("tenant_actor_id", None)
        if user.company_id and getattr(user, "database_url", None):
            actor_id = _resolve_tenant_actor_id(
                master_db,
                database_url=user.database_url,
                company_id=user.company_id,
                master_user_id=user.id,
                email=user.email,
            )
            if actor_id is not None:
                request.session["tenant_actor_id"] = actor_id
            else:
                request.session.clear()
                return templates.TemplateResponse(
                    "login.html",
                    {
                        "request": request,
                        "error": "La empresa todavía no tiene preparada la identidad local.",
                        "message": None,
                        "next_url": next_url,
                        "login_branding": branding_to_dict(default_branding_payload()),
                    },
                    status_code=503,
                )
    bind_server_session_context(
        master_db,
        request,
        company_id=request.session.get("company_id"),
        membership_id=request.session.get("membership_id"),
    )

    if next_url == DEFAULT_LOGIN_DESTINATION and getattr(user, "database_url", None):
        TenantSession = tenant_db_session(user.database_url)
        tenant_db = TenantSession()
        try:
            if not get_setup_status(tenant_db, user.company_id).is_operational:
                next_url = "/setup"
        finally:
            tenant_db.close()

    company = master_db.get(MasterCompany, user.company_id) if user.company_id else None
    settings = get_settings()

    logger.info(
        "Login correcto: user_id=%s email=%s company_id=%s company=%s env=%s",
        user.id,
        user.email,
        user.company_id,
        company.name if company else "",
        settings.environment,
    )

    return RedirectResponse(next_url, status_code=303)


@router.get("/select-company")
def select_company_page(request: Request, master_db: Session = Depends(get_master_db)):
    session = request.scope.get("session") or {}
    user_id = session.get("user_id")
    if not user_id or not session.get("pending_company_selection"):
        return RedirectResponse("/login", status_code=303)
    user = master_db.get(MasterUser, user_id)
    memberships = master_db.scalars(
        select(CompanyMembership)
        .options(selectinload(CompanyMembership.company))
        .where(
            CompanyMembership.user_id == user_id,
            CompanyMembership.is_active.is_(True),
            MasterCompany.active.is_(True),
            MasterCompany.status == "active",
        )
        .join(MasterCompany, MasterCompany.id == CompanyMembership.company_id)
        .order_by(CompanyMembership.is_owner.desc(), CompanyMembership.id.asc())
    ).all()
    if not user or not user.is_active or not memberships:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "select_company.html",
        {
            "request": request,
            "user": user,
            "memberships": memberships,
            "next_url": safe_internal_next(session.get("login_next") or DEFAULT_LOGIN_DESTINATION),
            "error": request.query_params.get("error"),
        },
    )


def _public_template(request: Request, template_name: str, **context):
    return templates.TemplateResponse(template_name, {"request": request, **context})


@router.get("/invitations/{token}")
def invitation_page(request: Request, token: str, master_db: Session = Depends(get_master_db)):
    return _public_template(request, "invitation.html", token=token, error=None)


@router.post("/invitations/{token}")
def invitation_accept(
    request: Request,
    token: str,
    full_name: str = Form(""),
    password: str = Form(""),
    master_db: Session = Depends(get_master_db),
):
    rate_decision = consume_public_action(master_db, request, scope="invitation", value=token)
    if not rate_decision.allowed:
        return _public_template(request, "invitation.html", token=token, error="Demasiados intentos. Espera unos minutos e inténtalo de nuevo.")
    try:
        accept_invitation(master_db, raw_token=token, full_name=full_name, password=password)
    except ValueError as exc:
        return _public_template(request, "invitation.html", token=token, error=str(exc))
    return RedirectResponse(f"/login?message={quote('Cuenta activada. Ya puedes iniciar sesión.')}", status_code=303)


@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return _public_template(request, "forgot_password.html", error=None, reset_link=None, submitted=False)


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    email: str = Form(...),
    master_db: Session = Depends(get_master_db),
):
    rate_decision = consume_public_action(master_db, request, scope="password-reset-request", value=email)
    if not rate_decision.allowed:
        return _public_template(request, "forgot_password.html", error=None, reset_link=None, submitted=True)
    raw_token = create_password_reset_token(master_db, email=email)
    settings = get_settings()
    if raw_token and settings.environment == "production":
        send_password_reset_email(
            master_db,
            email=email,
            raw_token=raw_token,
            base_url=str(request.base_url),
        )
    reset_link = f"/reset-password/{raw_token}" if raw_token and settings.environment != "production" else None
    return _public_template(request, "forgot_password.html", error=None, reset_link=reset_link, submitted=True)


@router.get("/reset-password/{token}")
def reset_password_page(request: Request, token: str):
    return _public_template(request, "reset_password.html", token=token, error=None)


@router.post("/reset-password/{token}")
def reset_password_submit(
    request: Request,
    token: str,
    password: str = Form(...),
    master_db: Session = Depends(get_master_db),
):
    rate_decision = consume_public_action(master_db, request, scope="password-reset-submit", value=token)
    if not rate_decision.allowed:
        response = _public_template(request, "reset_password.html", token=token, error="Demasiados intentos. Espera unos minutos e inténtalo de nuevo.")
        response.status_code = 429
        response.headers["Retry-After"] = str(rate_decision.retry_after)
        return response
    try:
        reset_password(master_db, raw_token=token, password=password)
    except ValueError as exc:
        return _public_template(request, "reset_password.html", token=token, error=str(exc))
    return RedirectResponse(f"/login?message={quote('Contraseña actualizada. Ya puedes iniciar sesión.')}", status_code=303)


@router.post("/select-company")
def select_company(
    request: Request,
    membership_id: int = Form(...),
    next: str = Form(DEFAULT_LOGIN_DESTINATION),
    master_db: Session = Depends(get_master_db),
):
    session = request.scope.get("session") or {}
    user_id = session.get("user_id")
    user = master_db.get(MasterUser, user_id) if user_id else None
    membership = master_db.scalar(
        select(CompanyMembership)
        .options(selectinload(CompanyMembership.company))
        .where(
            CompanyMembership.id == membership_id,
            CompanyMembership.user_id == user_id,
            CompanyMembership.is_active.is_(True),
            MasterCompany.active.is_(True),
            MasterCompany.status == "active",
        )
        .join(MasterCompany, MasterCompany.id == CompanyMembership.company_id)
    ) if user_id else None
    if not user or not user.is_active or not membership or not membership.company.active:
        return RedirectResponse("/select-company?error=Empresa no disponible", status_code=303)
    request.session["company_id"] = membership.company_id
    request.session["membership_id"] = membership.id
    request.session["company_slug"] = membership.company.slug
    request.session["tenant_session_version"] = user.session_version
    request.session.pop("tenant_actor_id", None)
    tenant_row = master_db.scalar(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == membership.company_id,
            MasterTenantDatabase.is_active.is_(True),
        )
    )
    if tenant_row and tenant_row.database_url:
        actor_id = _resolve_tenant_actor_id(
            master_db,
            database_url=tenant_row.database_url,
            company_id=membership.company_id,
            master_user_id=user.id,
            email=user.email,
        )
        if actor_id is not None:
            request.session["tenant_actor_id"] = actor_id
        else:
            request.session.clear()
            raise HTTPException(status_code=503, detail="La empresa todavía no tiene preparada la identidad local.")
    else:
        request.session.clear()
        raise HTTPException(status_code=503, detail="La empresa todavía no tiene una base operativa disponible.")
    rotate_server_session(
        master_db,
        request,
        user,
        company_id=membership.company_id,
        membership_id=membership.id,
    )
    request.session.pop("pending_company_selection", None)
    request.session.pop("login_next", None)
    return RedirectResponse(safe_internal_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    # The dependency is intentionally not required so logout remains
    # idempotent even after an expired tenant session.
    from app.master.database import MasterSessionLocal

    db = MasterSessionLocal()
    try:
        revoke_server_session(db, request)
    finally:
        db.close()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
