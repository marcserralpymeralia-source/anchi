from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_schema_for_engine(engine)


def ensure_schema() -> None:
    ensure_schema_for_engine(engine)


def ensure_schema_for_engine(target_engine) -> None:
    from app.db import models  # noqa: F401
    from app.migrations.runner import run_migration_plan
    from app.tenancy.migrations import TENANT_SCHEMA_MIGRATIONS, TenantSchemaMigration

    Base.metadata.create_all(bind=target_engine)
    session_factory = sessionmaker(bind=target_engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        run_migration_plan(target_engine, session, TenantSchemaMigration, TENANT_SCHEMA_MIGRATIONS, dry_run=False)
    finally:
        session.close()
