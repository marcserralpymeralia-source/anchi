from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import datetime, timezone
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer, CustomerAlias, CustomerContactPoint, CustomerDomain, Product, ProductAlias


CONFLICT_POLICIES = {"create_only", "update_existing", "skip_existing", "error_on_conflict"}


@dataclass(slots=True)
class UpsertOutcome:
    entity: Customer | Product | None
    action: str
    matched_by: str = ""


def normalize_conflict_policy(value: str | None) -> str:
    mapping = {
        "create_update": "update_existing",
        "create": "create_only",
        "update": "update_existing",
        "skip": "skip_existing",
        "skip_existing": "skip_existing",
        "create_only": "create_only",
        "update_existing": "update_existing",
        "error_on_conflict": "error_on_conflict",
    }
    normalized = mapping.get((value or "").strip().lower(), "update_existing")
    return normalized if normalized in CONFLICT_POLICIES else "update_existing"


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def split_multi_values(value: str) -> list[str]:
    raw = (value or "").replace("\n", ",").replace("\r", ",").replace(";", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_phone(value: str) -> str:
    return re.sub(r"[\s\-.()]+", "", (value or "").strip())


def is_valid_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (value or "").strip()))


def is_valid_phone(value: str) -> bool:
    digits = re.sub(r"\D+", "", value or "")
    return len(digits) >= 7


def name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_name(left), normalize_name(right)).ratio()


def _set_value_if_present(target: object, field: str, data: dict[str, str]) -> None:
    value = data.get(field)
    if value is not None and value != "":
        setattr(target, field, value)


def apply_customer_data(customer: Customer, data: dict[str, str]) -> None:
    for field in [
        "code",
        "fiscal_name",
        "commercial_name",
        "tax_id",
        "primary_email",
        "phone",
        "address",
        "city",
        "province",
        "country",
        "delegation",
        "assigned_salesperson",
        "accounting_code",
        "category",
        "notes",
    ]:
        _set_value_if_present(customer, field, data)
    if data.get("company_inactive"):
        customer.company_inactive = data["company_inactive"].strip().lower() in {"1", "si", "sí", "s", "yes", "true", "x", "inactive", "inactivo", "baja"}
    if data.get("status"):
        customer.status = data["status"]
    elif customer.company_inactive:
        customer.status = "inactive"
    notes_fragments: list[str] = []
    for key, label in [
        ("payment_terms", "Forma de pago"),
        ("tariff", "Tarifa"),
        ("customer_group", "Grupo"),
        ("internal_notes", "Notas internas"),
        ("conditions", "Condiciones"),
        ("useful_comments", "Comentarios"),
        ("habitual_channel", "Canal habitual"),
        ("contact_name", "Contacto"),
        ("contact_role", "Cargo"),
    ]:
        if data.get(key):
            notes_fragments.append(f"{label}: {data[key]}")
    if notes_fragments:
        current_notes = (customer.notes or "").strip()
        extra = " | ".join(notes_fragments)
        customer.notes = f"{current_notes} | {extra}".strip(" |") if current_notes else extra


def apply_product_data(product: Product, data: dict[str, str]) -> None:
    for field in [
        "reference",
        "alternative_code",
        "name",
        "description_cont",
        "brand",
        "usual_supplier",
        "family",
        "subfamily",
        "sale_unit",
        "size_group",
        "colors",
        "entry_date",
        "article_type",
        "replenishment_warehouse",
        "warehouse_location_code",
        "notes",
        "ean",
        "description",
    ]:
        _set_value_if_present(product, field, data)
    if data.get("name"):
        product.description = data["name"]
    if data.get("sale_price"):
        product.sale_price = _safe_float(data["sale_price"])
    if data.get("discount_percent"):
        product.discount_percent = _safe_float(data["discount_percent"])
    if data.get("obsolete"):
        product.obsolete = data["obsolete"].strip().lower() in {"1", "si", "sí", "s", "yes", "true", "x", "inactive", "inactivo", "baja", "obsoleto"}
    if data.get("status"):
        product.status = data["status"]
    elif product.obsolete:
        product.status = "inactive"


def _safe_float(value: str) -> float | None:
    if not value:
        return None
    normalized = value.replace("%", "").strip()
    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def replace_customer_aliases(db: Session, *, company_id: int, customer_id: int, aliases: str) -> None:
    db.query(CustomerAlias).filter(CustomerAlias.customer_id == customer_id).delete()
    for alias in split_multi_values(aliases):
        db.add(CustomerAlias(company_id=company_id, customer_id=customer_id, alias=alias))


def replace_customer_domains(db: Session, *, company_id: int, customer_id: int, domains: str) -> None:
    db.query(CustomerDomain).filter(CustomerDomain.customer_id == customer_id).delete()
    for domain in split_multi_values(domains):
        db.add(CustomerDomain(company_id=company_id, customer_id=customer_id, domain=domain.lower()))


def replace_customer_contact_points(
    db: Session,
    *,
    customer: Customer,
    emails: str = "",
    phones: str = "",
    domains: str = "",
    aliases: str = "",
) -> None:
    db.query(CustomerContactPoint).filter(CustomerContactPoint.customer_id == customer.id).delete()
    now = datetime.now(timezone.utc)
    seen: set[tuple[str, str]] = set()

    def add_point(point_type: str, value: str, *, is_primary: bool, label: str, source: str = "manual") -> None:
        normalized = value.strip().lower() if point_type in {"email", "domain", "alias"} else normalize_phone(value)
        if not normalized:
            return
        key = (point_type, normalized)
        if key in seen:
            return
        seen.add(key)
        existing = db.scalar(
            select(CustomerContactPoint).where(
                CustomerContactPoint.company_id == customer.company_id,
                CustomerContactPoint.type == point_type,
                CustomerContactPoint.value == normalized,
            )
        )
        if existing:
            if existing.customer_id != customer.id:
                return
            existing.label = label
            existing.contact_name = existing.contact_name or None
            existing.contact_role = existing.contact_role or None
            existing.is_primary = is_primary
            existing.active = True
            existing.source = source
            existing.last_seen_at = now
            existing.updated_at = now
            return
        db.add(
            CustomerContactPoint(
                company_id=customer.company_id,
                customer_id=customer.id,
                type=point_type,
                value=normalized,
                label=label,
                is_primary=is_primary,
                active=True,
                confidence=0.9 if is_primary else 0.7,
                source=source,
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )

    if customer.primary_email:
        add_point("email", customer.primary_email, is_primary=True, label="principal")
    if customer.phone:
        add_point("phone", customer.phone, is_primary=True, label="principal")
    for email in split_multi_values(emails):
        if is_valid_email(email):
            add_point("email", email, is_primary=False, label="asociado")
    for phone in split_multi_values(phones):
        if is_valid_phone(phone):
            add_point("phone", phone, is_primary=False, label="asociado")
    for domain in split_multi_values(domains):
        add_point("domain", domain, is_primary=False, label="dominio")
    for alias in split_multi_values(aliases):
        add_point("alias", alias, is_primary=False, label="alias")


def replace_product_aliases(db: Session, *, company_id: int, product_id: int, aliases: str) -> None:
    db.query(ProductAlias).filter(ProductAlias.product_id == product_id).delete()
    for alias in split_multi_values(aliases):
        db.add(ProductAlias(company_id=company_id, product_id=product_id, alias=alias))


def find_customer_match(db: Session, company_id: int, data: dict[str, str]) -> tuple[Customer | None, str]:
    code = (data.get("code") or "").strip()
    tax_id = (data.get("tax_id") or "").strip()
    primary_email = normalize_email(data.get("primary_email") or "") if data.get("primary_email") else ""
    phones = [normalize_phone(value) for value in split_multi_values(data.get("phone") or "") if value]
    domains = [value.lower() for value in split_multi_values(data.get("domains") or "")]
    name = (data.get("fiscal_name") or data.get("commercial_name") or "").strip()
    if code:
        customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.code == code))
        if customer:
            return customer, "code"
    if tax_id:
        customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.tax_id == tax_id))
        if customer:
            return customer, "tax_id"
    if primary_email:
        customer = (
            db.query(Customer)
            .join(CustomerContactPoint, CustomerContactPoint.customer_id == Customer.id)
            .filter(
                Customer.company_id == company_id,
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == primary_email,
            )
            .one_or_none()
        )
        if customer:
            return customer, "email"
    for domain in domains:
        customer = (
            db.query(Customer)
            .join(CustomerDomain, CustomerDomain.customer_id == Customer.id)
            .filter(Customer.company_id == company_id, CustomerDomain.company_id == company_id, CustomerDomain.domain == domain)
            .one_or_none()
        )
        if customer:
            return customer, "domain"
    for phone in phones:
        customer = (
            db.query(Customer)
            .join(CustomerContactPoint, CustomerContactPoint.customer_id == Customer.id)
            .filter(
                Customer.company_id == company_id,
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type.in_(["phone", "whatsapp"]),
                CustomerContactPoint.value == phone,
            )
            .one_or_none()
        )
        if customer:
            return customer, "phone"
    if name:
        candidates = db.scalars(select(Customer).where(Customer.company_id == company_id)).all()
        for customer in candidates:
            if name_similarity(customer.fiscal_name, name) >= 0.92 or (customer.commercial_name and name_similarity(customer.commercial_name, name) >= 0.92):
                return customer, "name"
    return None, ""


def find_product_match(db: Session, company_id: int, data: dict[str, str]) -> tuple[Product | None, str]:
    reference = (data.get("reference") or "").strip()
    alternative_code = (data.get("alternative_code") or "").strip()
    name = (data.get("name") or "").strip()
    if reference:
        product = db.scalar(select(Product).where(Product.company_id == company_id, Product.reference == reference))
        if product:
            return product, "reference"
    if alternative_code:
        product = db.scalar(select(Product).where(Product.company_id == company_id, Product.alternative_code == alternative_code))
        if product:
            return product, "alternative_code"
    if name:
        candidates = db.scalars(select(Product).where(Product.company_id == company_id)).all()
        for product in candidates:
            if name_similarity(product.name, name) >= 0.94 or (product.description and name_similarity(product.description, name) >= 0.94):
                return product, "name"
    return None, ""


def upsert_customer(
    db: Session,
    *,
    company_id: int,
    data: dict[str, str],
    source: str = "manual",
    actor_id: int | None = None,
    customer_id: int | None = None,
    conflict_policy: str = "update_existing",
) -> UpsertOutcome:
    policy = normalize_conflict_policy(conflict_policy)
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer and (customer.company_id != company_id or customer.deleted_at is not None):
        raise ValueError("Cliente no disponible para la compañía indicada.")
    matched_by = ""
    if not customer:
        customer, matched_by = find_customer_match(db, company_id, data)
    if customer and policy in {"skip_existing", "create_only"} and customer_id is None:
        return UpsertOutcome(customer, "skipped", matched_by=matched_by)
    if customer and policy == "error_on_conflict" and customer_id is None:
        raise ValueError(f"Cliente existente detectado por {matched_by or 'criterio de duplicado'}.")
    created = customer is None
    if not customer:
        fallback_code = (data.get("code") or data.get("fiscal_name") or data.get("commercial_name") or "").strip()
        if not fallback_code:
            raise ValueError("Faltan codigo o razon social para crear el cliente.")
        customer = Customer(company_id=company_id, code=(data.get("code") or fallback_code[:80]), fiscal_name=(data.get("fiscal_name") or data.get("commercial_name") or fallback_code))
        db.add(customer)
        db.flush()
    apply_customer_data(customer, data)
    replace_customer_aliases(db, company_id=company_id, customer_id=customer.id, aliases=data.get("aliases", ""))
    replace_customer_domains(db, company_id=company_id, customer_id=customer.id, domains=data.get("domains", ""))
    replace_customer_contact_points(
        db,
        customer=customer,
        emails=f"{data.get('primary_email', '')},{data.get('associated_emails', '')}",
        phones=data.get("associated_phones", ""),
        domains=data.get("domains", ""),
        aliases=data.get("aliases", ""),
    )
    return UpsertOutcome(customer, "created" if created else "updated", matched_by=matched_by)


def upsert_product(
    db: Session,
    *,
    company_id: int,
    data: dict[str, str],
    source: str = "manual",
    actor_id: int | None = None,
    product_id: int | None = None,
    conflict_policy: str = "update_existing",
) -> UpsertOutcome:
    policy = normalize_conflict_policy(conflict_policy)
    product = db.get(Product, product_id) if product_id else None
    if product and (product.company_id != company_id or product.deleted_at is not None):
        raise ValueError("Producto no disponible para la compañía indicada.")
    matched_by = ""
    if not product:
        product, matched_by = find_product_match(db, company_id, data)
    if product and policy in {"skip_existing", "create_only"} and product_id is None:
        return UpsertOutcome(product, "skipped", matched_by=matched_by)
    if product and policy == "error_on_conflict" and product_id is None:
        raise ValueError(f"Producto existente detectado por {matched_by or 'criterio de duplicado'}.")
    created = product is None
    if not product:
        fallback_reference = (data.get("reference") or data.get("name") or "").strip()
        if not fallback_reference:
            raise ValueError("Faltan referencia o nombre para crear el producto.")
        product = Product(company_id=company_id, reference=(data.get("reference") or fallback_reference[:100]), name=(data.get("name") or fallback_reference))
        db.add(product)
        db.flush()
    apply_product_data(product, data)
    replace_product_aliases(db, company_id=company_id, product_id=product.id, aliases=data.get("aliases", ""))
    return UpsertOutcome(product, "created" if created else "updated", matched_by=matched_by)
