from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import Base, ensure_schema_for_engine
from app.master.database import get_master_db
from app.master.models import MasterTenantDatabase
from app.master.service import load_tenant_context


def _connect_args(database_url: str) -> dict[str, object]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


@lru_cache(maxsize=128)
def get_tenant_engine(database_url: str):
    return create_engine(database_url, connect_args=_connect_args(database_url))


def tenant_db_session(database_url: str):
    engine = get_tenant_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_tenant_schema(database_url: str) -> None:
    engine = get_tenant_engine(database_url)
    Base.metadata.create_all(bind=engine)
    ensure_schema_for_engine(engine)


def _tenant_database_url(request: Request, master_db: Session) -> str | None:
    tenant = getattr(request.state, "tenant", None)
    if tenant and getattr(tenant.company, "database_url", None):
        return tenant.company.database_url

    session = request.scope.get("session") or {}
    company_id = session.get("company_id")
    if company_id:
        tenant_db = master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == company_id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )
        if tenant_db and tenant_db.database_url:
            return tenant_db.database_url
    tenant_context = load_tenant_context(request, master_db)
    if tenant_context and tenant_context.company.database_url:
        request.state.tenant = tenant_context
        return tenant_context.company.database_url
    return None


def get_tenant_db(request: Request, master_db: Session = Depends(get_master_db)) -> Generator[Session, None, None]:
    database_url = _tenant_database_url(request, master_db)
    if not database_url:
        session = request.scope.get("session") or {}
        if not any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug")):
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tenant no disponible")

    SessionFactory = tenant_db_session(database_url)
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()
