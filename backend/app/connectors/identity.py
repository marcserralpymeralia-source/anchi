from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer, ExternalIdentity, Product


class ExternalIdentityConflict(ValueError):
    pass


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def find_external_identity(
    db: Session,
    *,
    company_id: int,
    entity_type: str,
    external_system: str,
    external_id: str,
) -> ExternalIdentity | None:
    return db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.company_id == company_id,
            ExternalIdentity.entity_type == _clean(entity_type),
            ExternalIdentity.external_system == _clean(external_system),
            ExternalIdentity.external_id == _clean(external_id),
        )
    )


def bind_external_identity(
    db: Session,
    *,
    company_id: int,
    entity_type: str,
    entity_id: int,
    external_system: str,
    external_id: str,
    external_subtype: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExternalIdentity:
    cleaned_type = _clean(entity_type)
    cleaned_system = _clean(external_system)
    cleaned_id = _clean(external_id)
    if not cleaned_type or not cleaned_system or not cleaned_id:
        raise ValueError("Una identidad externa necesita tipo, sistema e identificador.")
    entity_model = {"customer": Customer, "product": Product}.get(cleaned_type)
    if entity_model is not None:
        entity = db.scalar(
            select(entity_model).where(
                entity_model.company_id == company_id,
                entity_model.id == entity_id,
            )
        )
        if entity is None:
            raise ExternalIdentityConflict("La entidad no pertenece al tenant de la identidad externa.")
    existing = find_external_identity(
        db,
        company_id=company_id,
        entity_type=cleaned_type,
        external_system=cleaned_system,
        external_id=cleaned_id,
    )
    if existing:
        if existing.entity_id != entity_id:
            raise ExternalIdentityConflict("La identidad externa ya pertenece a otra entidad del tenant.")
        if external_subtype is not None:
            existing.external_subtype = external_subtype
        if metadata is not None:
            existing.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        existing.updated_at = datetime.now(timezone.utc)
        return existing
    identity = ExternalIdentity(
        company_id=company_id,
        entity_type=cleaned_type,
        entity_id=entity_id,
        external_system=cleaned_system,
        external_id=cleaned_id,
        external_subtype=external_subtype,
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata is not None else None,
    )
    db.add(identity)
    db.flush()
    return identity


def resolve_customer_by_external_identity(db: Session, *, company_id: int, external_system: str, external_id: str) -> Customer | None:
    identity = find_external_identity(
        db,
        company_id=company_id,
        entity_type="customer",
        external_system=external_system,
        external_id=external_id,
    )
    if not identity:
        return None
    return db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.id == identity.entity_id))


def resolve_product_by_external_identity(db: Session, *, company_id: int, external_system: str, external_id: str) -> Product | None:
    identity = find_external_identity(
        db,
        company_id=company_id,
        entity_type="product",
        external_system=external_system,
        external_id=external_id,
    )
    if not identity:
        return None
    return db.scalar(select(Product).where(Product.company_id == company_id, Product.id == identity.entity_id))
