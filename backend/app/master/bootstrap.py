from sqlalchemy.orm import Session

from app.legacy.sync import sync_master_from_legacy_db


def bootstrap_master(master_db: Session, legacy_db: Session) -> dict[str, int]:
    return sync_master_from_legacy_db(master_db, legacy_db)
