from sqlalchemy.orm import Session

from app.master.service import TenantUser, authenticate_master_user


def authenticate_user(db: Session, email: str, password: str) -> TenantUser | None:
    return authenticate_master_user(db, email, password)
