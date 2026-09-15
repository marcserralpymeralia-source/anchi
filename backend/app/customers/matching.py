"""Shared customer matching helpers for channel contacts."""

from __future__ import annotations

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Customer, CustomerContact, CustomerContactPoint


_PHONE_CONTACT_POINT_TYPES = {
    "mobile",
    "movil",
    "phone",
    "telephone",
    "tel",
    "telefono",
    "whatsapp",
}


def _is_phone_contact_point(contact_type: str | None) -> bool:
    normalized = str(contact_type or "").strip().lower()
    return normalized in _PHONE_CONTACT_POINT_TYPES or any(
        token in normalized for token in ("phone", "whatsapp", "tel", "movil", "telefono")
    )


def normalize_phone_number(value: str | None) -> str:
    """Return the comparable digits from a phone number or WhatsApp address."""
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _add_phone(index: dict[str, int | None], value: str | None, customer_id: int) -> None:
    phone = normalize_phone_number(value)
    if not phone:
        return
    previous = index.get(phone)
    if phone in index and previous != customer_id:
        # Do not assign a shared phone to an arbitrary customer.
        index[phone] = None
        return
    index[phone] = customer_id


def build_customer_phone_index(db: Session, company_id: int) -> dict[str, int | None]:
    """Build a tenant-scoped index from every active customer phone source."""
    customers = db.scalars(
        select(Customer).where(
            Customer.company_id == company_id,
            Customer.deleted_at.is_(None),
            Customer.status != "inactive",
            or_(Customer.company_inactive.is_(False), Customer.company_inactive.is_(None)),
        )
    ).all()
    if not customers:
        return {}

    customer_ids = [customer.id for customer in customers]
    index: dict[str, int | None] = {}
    for customer in customers:
        _add_phone(index, customer.phone, customer.id)

    for customer_id, phone in db.execute(
        select(CustomerContact.customer_id, CustomerContact.phone).where(
            CustomerContact.company_id == company_id,
            CustomerContact.customer_id.in_(customer_ids),
            CustomerContact.phone.is_not(None),
        )
    ).all():
        _add_phone(index, phone, customer_id)

    for customer_id, contact_type, value in db.execute(
        select(CustomerContactPoint.customer_id, CustomerContactPoint.type, CustomerContactPoint.value).where(
            CustomerContactPoint.company_id == company_id,
            CustomerContactPoint.customer_id.in_(customer_ids),
            CustomerContactPoint.active.is_(True),
        )
    ).all():
        if _is_phone_contact_point(contact_type):
            _add_phone(index, value, customer_id)
    return index


def customer_id_from_phone(phone: str | None, phone_index: dict[str, int | None]) -> int | None:
    """Find a customer by exact international number, with a safe local suffix fallback."""
    normalized = normalize_phone_number(phone)
    if not normalized:
        return None

    if normalized in phone_index:
        # An exact duplicate is deliberately kept unresolved by _add_phone.
        return phone_index[normalized]

    if len(normalized) < 9:
        return None
    suffix = normalized[-9:]
    suffix_matches = {
        customer_id
        for candidate, customer_id in phone_index.items()
        if customer_id and len(candidate) >= 9 and candidate[-9:] == suffix
    }
    return next(iter(suffix_matches)) if len(suffix_matches) == 1 else None


def resolve_customer_id_by_phone(db: Session, company_id: int, phone: str | None) -> int | None:
    return customer_id_from_phone(phone, build_customer_phone_index(db, company_id))
