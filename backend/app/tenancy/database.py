from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.master.database import get_master_db
from app.master.service import load_tenant_context
from app.db.database import Base
from app.tenancy.migrations import upgrade_tenant_schema


def _connect_args(database_url: str) -> dict[str, object]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


@lru_cache(maxsize=128)
def get_tenant_engine(database_url: str):
    return create_engine(database_url, connect_args=_connect_args(database_url))


def tenant_db_session(database_url: str):
    engine = get_tenant_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _infer_company_id(engine) -> int | None:  # noqa: ANN001
    with engine.connect() as conn:
        inspector = inspect(conn)
        if "companies" not in inspector.get_table_names():
            return None
        value = conn.execute(text("SELECT id FROM companies ORDER BY id ASC LIMIT 1")).scalar()
    return int(value) if value is not None else None


def ensure_tenant_schema(database_url: str, *, company_id: int | None = None, application_version: str | None = None, baseline: bool = False) -> dict:
    engine = get_tenant_engine(database_url)
    Base.metadata.create_all(bind=engine)
    resolved_company_id = company_id if company_id is not None else _infer_company_id(engine)
    if resolved_company_id is None:
        resolved_company_id = 1
    return upgrade_tenant_schema(engine, company_id=resolved_company_id, application_version=application_version, baseline=baseline)


def get_tenant_db(request: Request, master_db: Session = Depends(get_master_db)) -> Generator[Session, None, None]:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        try:
            tenant = load_tenant_context(request, master_db)
        except SQLAlchemyError:
            session = request.scope.get("session") or {}
            if any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug")):
                if isinstance(session, dict):
                    session.clear()
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
        if tenant:
            request.state.tenant = tenant

    database_url = tenant.company.database_url if tenant else None
    if not database_url:
        session = request.scope.get("session") or {}
        if any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug")):
            if isinstance(session, dict):
                session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    try:
        ensure_tenant_schema(database_url, company_id=tenant.company.id)
        SessionFactory = tenant_db_session(database_url)
        db = SessionFactory()
    except SQLAlchemyError:
        session = request.scope.get("session") or {}
        if any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug")):
            if isinstance(session, dict):
                session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    try:
        yield db
    finally:
        db.close()
