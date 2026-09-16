from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import EmailSettings
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, PasswordResetToken, UserInvitation, utcnow
from app.settings.integrations import send_test_email
from app.superadmin.service import TENANT_ROLES, create_company_user, normalize_email
from app.tenancy.database import tenant_db_session


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_password(password: str) -> str:
    value = (password or "").strip()
    if len(value) < 12:
        raise ValueError("La contraseña debe tener al menos 12 caracteres")
    return value


def _expired(value, now) -> bool:  # noqa: ANN001
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=now.tzinfo)
    return value <= now


def create_invitation(
    master_db: Session,
    *,
    company_id: int,
    email: str,
    role_key: str,
    invited_by_user_id: int | None,
    expires_hours: int = 72,
) -> tuple[UserInvitation, str]:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise ValueError("Introduce un email válido")
    if role_key not in TENANT_ROLES:
        raise ValueError("Rol no válido")
    company = master_db.get(MasterCompany, company_id)
    if not company or not company.active:
        raise ValueError("La empresa no está disponible")
    now = utcnow()
    pending = master_db.scalars(
        select(UserInvitation).where(
            UserInvitation.company_id == company_id,
            UserInvitation.email == normalized,
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
        )
    ).all()
    for previous in pending:
        previous.revoked_at = now
    raw_token = secrets.token_urlsafe(32)
    invitation = UserInvitation(
        company_id=company_id,
        email=normalized,
        role_key=role_key,
        token_hash=_token_digest(raw_token),
        invited_by_user_id=invited_by_user_id,
        expires_at=now + timedelta(hours=max(1, min(expires_hours, 168))),
    )
    master_db.add(invitation)
    master_db.commit()
    master_db.refresh(invitation)
    return invitation, raw_token


def accept_invitation(
    master_db: Session,
    *,
    raw_token: str,
    full_name: str,
    password: str,
) -> MasterUser:
    invitation = master_db.scalar(select(UserInvitation).where(UserInvitation.token_hash == _token_digest(raw_token)))
    now = utcnow()
    if not invitation or invitation.accepted_at or invitation.revoked_at or _expired(invitation.expires_at, now):
        raise ValueError("La invitación no es válida o ha caducado")
    company = master_db.get(MasterCompany, invitation.company_id)
    if not company or not company.active:
        raise ValueError("La empresa ya no está disponible")

    existing = master_db.scalar(select(MasterUser).where(MasterUser.email == invitation.email))
    if existing is None:
        password_value = _validate_password(password)
        full_name_value = (full_name or invitation.email.split("@", 1)[0]).strip()[:200]
        create_company_user(
            master_db,
            company_id=invitation.company_id,
            full_name=full_name_value,
            email=invitation.email,
            password=password_value,
            role_key=invitation.role_key,
            actor_user_id=invitation.invited_by_user_id,
        )
        existing = master_db.scalar(select(MasterUser).where(MasterUser.email == invitation.email))
    else:
        if not existing.is_active:
            raise ValueError("La cuenta está desactivada")
        membership = master_db.scalar(
            select(CompanyMembership).where(
                CompanyMembership.user_id == existing.id,
                CompanyMembership.company_id == invitation.company_id,
            )
        )
        if membership is None:
            membership = CompanyMembership(user_id=existing.id, company_id=invitation.company_id, role_key=invitation.role_key, is_active=True)
            master_db.add(membership)
        else:
            membership.role_key = invitation.role_key
            membership.is_active = True
        existing.email_verified = True
        existing.session_version = (existing.session_version or 1) + 1
        master_db.commit()

    if existing is None:  # pragma: no cover - defensive guard for a failed provisioning transaction
        raise ValueError("No se pudo crear la cuenta")
    existing.email_verified = True
    invitation.accepted_at = now
    master_db.commit()
    tenant_row = master_db.scalar(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == invitation.company_id,
            MasterTenantDatabase.is_active.is_(True),
        )
    )
    if tenant_row is not None and tenant_row.database_url:
        # Existing MasterUsers need the same local actor projection as newly
        # provisioned users before they can create jobs or audit events.
        from app.master.provisioning import synchronize_tenant_actors

        synchronize_tenant_actors(master_db, tenant_row)
    return existing


def create_password_reset_token(master_db: Session, *, email: str, expires_hours: int = 1) -> str | None:
    normalized = normalize_email(email)
    user = master_db.scalar(select(MasterUser).where(MasterUser.email == normalized, MasterUser.is_active.is_(True)))
    if not user:
        return None
    now = utcnow()
    for previous in master_db.scalars(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))).all():
        previous.used_at = now
    raw_token = secrets.token_urlsafe(32)
    master_db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_token_digest(raw_token),
            expires_at=now + timedelta(hours=max(1, min(expires_hours, 24))),
        )
    )
    master_db.commit()
    return raw_token


def send_password_reset_email(master_db: Session, *, email: str, raw_token: str, base_url: str) -> bool:
    """Deliver a reset link using the first active tenant SMTP configuration.

    The control plane never stores SMTP credentials. They remain in the
    tenant database and are read only for the duration of this operation.
    """

    normalized = normalize_email(email)
    user = master_db.scalar(select(MasterUser).where(MasterUser.email == normalized, MasterUser.is_active.is_(True)))
    if user is None:
        return False
    memberships = master_db.scalars(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.is_active.is_(True),
        ).order_by(CompanyMembership.is_owner.desc(), CompanyMembership.id.asc())
    ).all()
    reset_link = f"{base_url.rstrip('/')}/reset-password/{raw_token}"
    message = (
        "Hemos recibido una solicitud para cambiar tu contraseña de Anchi.\n\n"
        f"Abre este enlace para continuar:\n{reset_link}\n\n"
        "El enlace caduca en una hora y solo puede utilizarse una vez."
    )
    for membership in memberships:
        tenant_row = master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == membership.company_id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )
        if tenant_row is None or not tenant_row.database_url:
            continue
        tenant_session = tenant_db_session(tenant_row.database_url)()
        try:
            settings = tenant_session.scalar(
                select(EmailSettings).where(EmailSettings.company_id == membership.company_id)
            )
            if settings is None:
                continue
            result = send_test_email(
                settings,
                normalized,
                "Recuperación de contraseña · Anchi",
                message,
            )
            if result.get("ok"):
                return True
        except Exception:  # noqa: BLE001
            # The public endpoint must remain indistinguishable for unknown
            # accounts and SMTP failures; do not leak provider diagnostics.
            continue
        finally:
            tenant_session.close()
    return False


def reset_password(master_db: Session, *, raw_token: str, password: str) -> MasterUser:
    password_value = _validate_password(password)
    reset_token = master_db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == _token_digest(raw_token)))
    now = utcnow()
    if not reset_token or reset_token.used_at or _expired(reset_token.expires_at, now):
        raise ValueError("El enlace de recuperación no es válido o ha caducado")
    user = master_db.get(MasterUser, reset_token.user_id)
    if not user or not user.is_active:
        raise ValueError("La cuenta no está disponible")
    user.password_hash = hash_password(password_value)
    user.password_version = (user.password_version or 1) + 1
    user.session_version = (user.session_version or 1) + 1
    reset_token.used_at = now
    master_db.commit()
    return user
