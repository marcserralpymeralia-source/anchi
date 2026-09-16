from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.auth.lifecycle import create_invitation
from app.auth.dependencies import require_tenant_role
from app.auth.rate_limit import consume_public_action
from app.master.database import get_master_db
from app.master.models import CompanyMembership, MasterUser, UserInvitation
from app.master.service import TenantUser
from app.superadmin.service import create_company_user
from app.db.models import Role, User
from app.logs.service import log_action
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/users", tags=["users"])


MANAGE_USER_ROLES = ("Administrador", "Superadmin")


def _local_actor(db: Session, user: TenantUser) -> User | None:
    """Resolve the tenant projection used by local foreign keys and audit logs."""

    actor = db.scalar(select(User).where(User.company_id == user.company_id, User.master_user_id == user.id))
    if actor is not None:
        return actor
    return db.scalar(select(User).where(User.company_id == user.company_id, User.id == user.id))


def _error_redirect(message: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/users?error={quote(message)}", status_code=303)


@router.get("")
def list_users(request: Request, db: Session = Depends(get_tenant_db), master_db: Session = Depends(get_master_db), user: TenantUser = Depends(require_tenant_role(*MANAGE_USER_ROLES))):
    users = db.scalars(select(User).where(User.company_id == user.company_id).order_by(User.name)).all()
    roles = db.scalars(select(Role).where(Role.company_id == user.company_id).order_by(Role.name)).all()
    invitations = master_db.scalars(
        select(UserInvitation)
        .where(UserInvitation.company_id == user.company_id, UserInvitation.accepted_at.is_(None), UserInvitation.revoked_at.is_(None))
        .order_by(UserInvitation.created_at.desc())
    ).all()
    invitation_link = request.session.pop("last_invitation_link", None)
    return templates.TemplateResponse("users/list.html", {"request": request, "user": user, "users": users, "roles": roles, "invitations": invitations, "invitation_link": invitation_link})


@router.post("")
def create_user(
    name: str = Form(...),
    email: str = Form(...),
    role_id: int = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_tenant_role(*MANAGE_USER_ROLES)),
):
    role = db.scalar(select(Role).where(Role.id == role_id, Role.company_id == user.company_id))
    if role is None:
        return _error_redirect("El rol seleccionado no pertenece a esta empresa")
    try:
        membership = create_company_user(
            master_db,
            company_id=user.company_id,
            full_name=name,
            email=email,
            password=password,
            role_key=role.name,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        return _error_redirect(str(exc))
    new_user = db.scalar(select(User).where(User.company_id == user.company_id, User.master_user_id == membership.user_id))
    actor = _local_actor(db, user)
    log_action(db, company_id=user.company_id, user=actor, action="user.create", entity_type="user", entity_id=new_user.id if new_user else None, message=f"Usuario creado: {email}")
    return RedirectResponse("/users", status_code=303)


@router.post("/invitations")
def invite_user(
    request: Request,
    email: str = Form(...),
    role_key: str = Form("Operador"),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_tenant_role(*MANAGE_USER_ROLES)),
):
    rate_decision = consume_public_action(
        master_db,
        request,
        scope="tenant-invitation",
        value=f"{user.company_id}:{email}",
    )
    if not rate_decision.allowed:
        return _error_redirect("Demasiadas invitaciones. Espera unos minutos e inténtalo de nuevo.")
    try:
        _invitation, raw_token = create_invitation(
            master_db,
            company_id=user.company_id,
            email=email,
            role_key=role_key,
            invited_by_user_id=user.id,
        )
    except ValueError as exc:
        return _error_redirect(str(exc))
    request.session["last_invitation_link"] = f"{str(request.base_url).rstrip('/')}/invitations/{raw_token}"
    return RedirectResponse("/users", status_code=303)


@router.post("/{user_id}")
def update_user(
    user_id: int,
    name: str = Form(...),
    role_id: int = Form(...),
    is_active: bool = Form(False),
    password: str = Form(""),
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_tenant_role(*MANAGE_USER_ROLES)),
):
    target = db.get(User, user_id)
    if target and target.company_id == user.company_id:
        role = db.scalar(select(Role).where(Role.id == role_id, Role.company_id == user.company_id))
        if role is None:
            return _error_redirect("El rol seleccionado no pertenece a esta empresa")
        membership = None
        master_user = master_db.get(MasterUser, target.master_user_id) if target.master_user_id else None
        if master_user:
            membership = master_db.scalar(
                select(CompanyMembership).where(
                    CompanyMembership.user_id == master_user.id,
                    CompanyMembership.company_id == user.company_id,
                )
            )
            if membership and membership.is_owner:
                if not is_active:
                    return _error_redirect("No puedes desactivar al propietario de la empresa. Transfiere la propiedad antes de continuar.")
                if role.name != "Administrador":
                    return _error_redirect("El propietario debe conservar el rol Administrador. Transfiere la propiedad antes de continuar.")
            master_user.full_name = name.strip()[:200] or master_user.full_name
            membership_changed = bool(membership and (membership.is_active != is_active or membership.role_key != role.name))
            master_user.is_active = is_active
            if membership_changed:
                master_user.session_version = (master_user.session_version or 1) + 1
            if password:
                from app.core.security import hash_password

                master_user.password_hash = hash_password(password)
                master_user.password_version = (master_user.password_version or 1) + 1
                master_user.session_version = (master_user.session_version or 1) + 1
        if membership:
            membership.role_key = role.name
            membership.is_active = is_active
        target.name = name
        target.role_id = role.id
        target.is_active = is_active
        if password:
            from app.core.security import hash_password

            target.password_hash = hash_password(password)
        db.commit()
        master_db.commit()
        actor = _local_actor(db, user)
        log_action(db, company_id=user.company_id, user=actor, action="user.update", entity_type="user", entity_id=target.id, message=f"Usuario actualizado: {target.email}")
    return RedirectResponse("/users", status_code=303)
