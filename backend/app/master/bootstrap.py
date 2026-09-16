import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.legacy.sync import sync_master_from_legacy_db
from app.master.models import MasterUser
from app.superadmin.service import normalize_email


def bootstrap_master(master_db: Session, legacy_db: Session) -> dict[str, int]:
    return sync_master_from_legacy_db(master_db, legacy_db)


def ensure_platform_admin(master_db: Session) -> MasterUser | None:
    """Ensure a local/demo bootstrap identity without creating production secrets."""

    settings = get_settings()
    configured_email = normalize_email(os.getenv("PLATFORM_ADMIN_EMAIL") or settings.default_admin_email)
    configured_password = os.getenv("PLATFORM_ADMIN_PASSWORD") or settings.default_admin_password
    is_non_production = settings.environment in {"development", "demo", "test"}
    # Test fixtures deliberately use the default admin email as a tenant user.
    # Do not silently turn that identity into a platform superadmin unless a
    # test explicitly opts into platform bootstrap.
    if settings.environment == "test" and not os.getenv("PLATFORM_ADMIN_PASSWORD"):
        return None
    if not configured_email or (settings.environment == "production" and not os.getenv("PLATFORM_ADMIN_PASSWORD")):
        return None

    user = master_db.scalar(select(MasterUser).where(MasterUser.email == configured_email))
    if user is None:
        if not is_non_production and not os.getenv("PLATFORM_ADMIN_PASSWORD"):
            return None
        user = MasterUser(
            email=configured_email,
            full_name="Superadmin de plataforma",
            password_hash=hash_password(configured_password),
            is_active=True,
            platform_role_key="superadmin",
            email_verified=True,
        )
        master_db.add(user)
    else:
        if user.platform_role_key != "superadmin" and configured_email == normalize_email(settings.default_admin_email) and is_non_production:
            user.platform_role_key = "superadmin"
            user.is_active = True
        elif is_non_production and user.platform_role_key == "superadmin":
            user.is_active = True
    master_db.commit()
    return user if user.platform_role_key == "superadmin" else None
