from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.master_database_url.startswith("sqlite") else {}
engine = create_engine(settings.master_database_url, connect_args=connect_args)
MasterSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MasterBase(DeclarativeBase):
    pass


def get_master_db() -> Generator[Session, None, None]:
    db = MasterSessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_master_db() -> None:
    from app.master import models  # noqa: F401

    MasterBase.metadata.create_all(bind=engine)
