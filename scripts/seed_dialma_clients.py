#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select

from app.db.database import SessionLocal, init_db  # noqa: E402
from app.db.models import Company, Customer, CustomerAlias, CustomerContactPoint, CustomerDomain, Role, User  # noqa: E402
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS  # noqa: E402
from app.core.security import hash_password  # noqa: E402


DEFAULT_COMPANY_NAME = "Dialma"
DEFAULT_ADMIN_EMAIL = "admin@dialma.local"
DEFAULT_ADMIN_PASSWORD = "DialmaDemo2026!"

CUSTOMERS = [
    {
        "code": "DIA-C001",
        "fiscal_name": "Hotel Costa Norte SL",
        "commercial_name": "Hotel Costa Norte",
        "primary_email": "compras@hotelcostanorte.demo",
        "phone": "977000101",
        "habitual_channel": "Email",
        "status": "active",
        "knowledge_note": "Suele enviar pedidos por email los lunes.",
        "aliases": ["Hotel Costa Norte"],
        "domains": ["hotelcostanorte.demo"],
        "associated_emails": ["recepcion@hotelcostanorte.demo"],
        "associated_phones": ["977000111"],
        "contact_name": "Marta Soler",
        "contact_role": "Compras",
        "city": "Tarragona",
        "province": "Tarragona",
        "category": "Hotel",
    },
    {
        "code": "DIA-C002",
        "fiscal_name": "Restaurante Brisa Mediterránea",
        "commercial_name": "Brisa Mediterránea",
        "primary_email": "pedidos@brisamediterranea.demo",
        "phone": "977000102",
        "habitual_channel": "WhatsApp",
        "status": "active",
        "knowledge_note": "Pedidos cortos e informales.",
        "aliases": ["Brisa Mediterránea", "Brisa Med"],
        "domains": [],
        "associated_emails": ["gerencia@brisamediterranea.demo"],
        "associated_phones": ["977000212"],
        "contact_name": "Javier Ruiz",
        "contact_role": "Gerencia",
        "city": "Reus",
        "province": "Tarragona",
        "category": "Restaurante",
    },
    {
        "code": "DIA-C003",
        "fiscal_name": "Cafetería Plaza Central",
        "commercial_name": "Cafetería Plaza Central",
        "primary_email": "administracion@plazacentral.demo",
        "phone": "977000103",
        "habitual_channel": "Email",
        "status": "active",
        "knowledge_note": "Cliente con pedidos sencillos y recurrentes.",
        "aliases": ["Plaza Central"],
        "domains": ["plazacentral.demo"],
        "associated_emails": ["info@plazacentral.demo"],
        "associated_phones": [],
        "contact_name": "Laura Vidal",
        "contact_role": "Administración",
        "city": "Cambrils",
        "province": "Tarragona",
        "category": "Cafetería",
    },
    {
        "code": "DIA-C004",
        "fiscal_name": "Distribuciones Ebro SL",
        "commercial_name": "Ebro",
        "primary_email": "compras@distribucionesebro.demo",
        "phone": "977000104",
        "habitual_channel": "Email",
        "status": "active",
        "knowledge_note": "Cliente con pedidos recurrentes y referencias internas.",
        "aliases": ["Ebro", "Distribuciones Ebro"],
        "domains": ["distribucionesebro.demo"],
        "associated_emails": ["logistica@distribucionesebro.demo"],
        "associated_phones": ["977000214", "977000215"],
        "contact_name": "Sergio Pons",
        "contact_role": "Compras",
        "city": "Lleida",
        "province": "Lleida",
        "category": "Distribución",
    },
    {
        "code": "DIA-C005",
        "fiscal_name": "Supermercados Delta",
        "commercial_name": "Supermercados Delta",
        "primary_email": "pedidos@supermercadosdelta.demo",
        "phone": "977000105",
        "habitual_channel": "Email",
        "status": "active",
        "knowledge_note": "Usa plantillas de pedido estables.",
        "aliases": ["Delta"],
        "domains": ["supermercadosdelta.demo"],
        "associated_emails": ["central@supermercadosdelta.demo"],
        "associated_phones": ["977000315"],
        "contact_name": "Nuria Ferrer",
        "contact_role": "Central de compras",
        "city": "Tortosa",
        "province": "Tarragona",
        "category": "Supermercado",
    },
    {
        "code": "DIA-C006",
        "fiscal_name": "Catering La Terraza",
        "commercial_name": "La Terraza",
        "primary_email": "cocina@cateringlaterraza.demo",
        "phone": "977000106",
        "habitual_channel": "WhatsApp",
        "status": "active",
        "knowledge_note": "Pedidos de última hora y cambios frecuentes.",
        "aliases": ["La Terraza", "Catering Terraza"],
        "domains": [],
        "associated_emails": [],
        "associated_phones": ["977000316"],
        "contact_name": "Cristina Bosch",
        "contact_role": "Operaciones",
        "city": "Salou",
        "province": "Tarragona",
        "category": "Catering",
    },
    {
        "code": "DIA-C007",
        "fiscal_name": "Panadería El Molino",
        "commercial_name": "El Molino",
        "primary_email": "pedidos@panaderiaelmolino.demo",
        "phone": "977000107",
        "habitual_channel": "Email",
        "status": "active",
        "knowledge_note": "Cliente de volumen pequeño pero muy estable.",
        "aliases": ["El Molino"],
        "domains": ["panaderiaelmolino.demo"],
        "associated_emails": ["tienda@panaderiaelmolino.demo"],
        "associated_phones": ["977000417"],
        "contact_name": "Pau Serra",
        "contact_role": "Tienda",
        "city": "Vila-seca",
        "province": "Tarragona",
        "category": "Panadería",
    },
    {
        "code": "DIA-C008",
        "fiscal_name": "Bar Puerto Viejo",
        "commercial_name": "Puerto Viejo",
        "primary_email": "contacto@barpuertoviejo.demo",
        "phone": "977000108",
        "habitual_channel": "Teléfono",
        "status": "active",
        "knowledge_note": "Pide por llamada y confirma cambios por mensaje.",
        "aliases": ["Puerto Viejo", "Bar del Puerto"],
        "domains": [],
        "associated_emails": ["reservas@barpuertoviejo.demo"],
        "associated_phones": ["977000518"],
        "contact_name": "Oriol Casals",
        "contact_role": "Encargado",
        "city": "Cambrils",
        "province": "Tarragona",
        "category": "Bar",
    },
    {
        "code": "DIA-C009",
        "fiscal_name": "Eventos Mar Blau",
        "commercial_name": "Mar Blau",
        "primary_email": "produccion@eventosmarblau.demo",
        "phone": "977000109",
        "habitual_channel": "Email",
        "status": "active",
        "knowledge_note": "Pedidos por evento, con picos de volumen.",
        "aliases": ["Mar Blau", "Eventos Blau"],
        "domains": ["eventosmarblau.demo"],
        "associated_emails": ["produccion2@eventosmarblau.demo"],
        "associated_phones": ["977000619"],
        "contact_name": "Mireia Grau",
        "contact_role": "Producción",
        "city": "Tarragona",
        "province": "Tarragona",
        "category": "Eventos",
    },
    {
        "code": "DIA-C010",
        "fiscal_name": "Cliente Inactivo Demo",
        "commercial_name": "Cliente Inactivo Demo",
        "primary_email": "antiguo@clienteinactivo.demo",
        "phone": "977000110",
        "habitual_channel": "Email",
        "status": "inactive",
        "company_inactive": True,
        "knowledge_note": "Cliente histórico sin actividad reciente.",
        "aliases": ["Inactivo Demo"],
        "domains": ["clienteinactivo.demo"],
        "associated_emails": ["archivo@clienteinactivo.demo"],
        "associated_phones": ["977000710"],
        "contact_name": "—",
        "contact_role": "—",
        "city": "Reus",
        "province": "Tarragona",
        "category": "Demo",
    },
]


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def iter_values(raw: list[str] | str | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        values = [piece.strip() for piece in raw.replace(";", ",").split(",")]
    return [value for value in (item.strip() for item in values) if value]


def ensure_company(db, name: str, admin_email: str) -> tuple[Company, bool]:
    company = db.scalar(select(Company).where(Company.name == name))
    created = False
    if not company:
        company = Company(
            name=name,
            legal_name=name,
            active=True,
            plan="client",
            currency="EUR",
            language="es",
            default_language="es",
            timezone="Europe/Madrid",
            date_format="%d/%m/%Y",
            decimal_separator=",",
            email=admin_email,
        )
        db.add(company)
        db.flush()
        created = True
    return company, created


def ensure_admin(db, company: Company, admin_email: str, admin_password: str) -> User:
    role = db.scalar(select(Role).where(Role.company_id == company.id, Role.name == "Administrador"))
    if not role:
        role = Role(company_id=company.id, name="Administrador", permissions=DEFAULT_ROLE_PERMISSIONS.get("Administrador", ""))
        db.add(role)
        db.flush()
    user = db.scalar(select(User).where(User.company_id == company.id, User.email == admin_email))
    if user:
        return user
    user = User(
        company_id=company.id,
        role_id=role.id,
        email=admin_email,
        name="Administrador",
        password_hash=hash_password(admin_password),
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def find_customer(db, company_id: int, row: dict) -> Customer | None:
    code = (row.get("code") or "").strip()
    tax_id = (row.get("tax_id") or "").strip()
    email = normalize_email(row.get("primary_email") or "") if row.get("primary_email") else ""
    if code:
        customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.code == code))
        if customer:
            return customer
    if tax_id:
        customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.tax_id == tax_id))
        if customer:
            return customer
    if email:
        customer = db.scalar(
            select(Customer)
            .join(CustomerContactPoint, CustomerContactPoint.customer_id == Customer.id)
            .where(
                Customer.company_id == company_id,
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == email,
            )
        )
        if customer:
            return customer
    return None


def upsert_contact_point(db, *, company_id: int, customer_id: int, type_: str, value: str, label: str | None = None, contact_name: str | None = None, contact_role: str | None = None, is_primary: bool = False) -> None:
    normalized = normalize_email(value) if type_ == "email" else normalize_phone(value) if type_ in {"phone", "whatsapp"} else value.strip().lower()
    existing = db.scalar(
        select(CustomerContactPoint).where(
            CustomerContactPoint.company_id == company_id,
            CustomerContactPoint.type == type_,
            CustomerContactPoint.value == normalized,
        )
    )
    now = datetime.now(timezone.utc)
    if existing:
        existing.customer_id = customer_id
        existing.label = label
        existing.contact_name = contact_name or existing.contact_name
        existing.contact_role = contact_role or existing.contact_role
        existing.is_primary = is_primary or existing.is_primary
        existing.active = True
        existing.updated_at = now
        existing.last_seen_at = now
        return
    db.add(
        CustomerContactPoint(
            company_id=company_id,
            customer_id=customer_id,
            type=type_,
            value=normalized,
            label=label,
            contact_name=contact_name,
            contact_role=contact_role,
            is_primary=is_primary,
            active=True,
            confidence=0.85 if is_primary else 0.65,
            source="seed",
            first_seen_at=now,
            last_seen_at=now,
        )
    )


def upsert_alias(db, *, company_id: int, customer_id: int, alias: str) -> None:
    alias = alias.strip()
    if not alias:
        return
    existing = db.scalar(select(CustomerAlias).where(CustomerAlias.company_id == company_id, CustomerAlias.alias == alias))
    if existing:
        existing.customer_id = customer_id
        return
    db.add(CustomerAlias(company_id=company_id, customer_id=customer_id, alias=alias))


def upsert_domain(db, *, company_id: int, customer_id: int, domain: str) -> None:
    domain = domain.strip().lower()
    if not domain:
        return
    existing = db.scalar(select(CustomerDomain).where(CustomerDomain.company_id == company_id, CustomerDomain.domain == domain))
    if existing:
        existing.customer_id = customer_id
        return
    db.add(CustomerDomain(company_id=company_id, customer_id=customer_id, domain=domain))


def apply_customer(db, *, company_id: int, row: dict) -> tuple[Customer, str]:
    customer = find_customer(db, company_id, row)
    created = False
    if not customer:
        customer = Customer(
            company_id=company_id,
            code=row["code"],
            fiscal_name=row["fiscal_name"],
        )
        db.add(customer)
        db.flush()
        created = True

    mutable_fields = [
        "code",
        "fiscal_name",
        "commercial_name",
        "primary_email",
        "delegation",
        "phone",
        "address",
        "city",
        "province",
        "country",
        "tax_id",
        "assigned_salesperson",
        "accounting_code",
        "category",
        "notes",
    ]
    for field in mutable_fields:
        value = row.get(field)
        if value is not None and value != "":
            setattr(customer, field, value)
    customer.status = row.get("status") or "active"
    customer.company_inactive = bool(row.get("company_inactive", False))
    if customer.company_inactive:
        customer.status = "inactive"

    notes = [row.get("knowledge_note", "").strip()]
    if row.get("habitual_channel"):
        notes.append(f"Canal habitual: {row['habitual_channel']}")
    notes = [item for item in notes if item]
    if notes:
        existing = (customer.notes or "").strip()
        merged = " | ".join(dict.fromkeys([piece for piece in ([existing] if existing else []) + notes if piece]))
        customer.notes = merged

    for alias in iter_values(row.get("aliases")):
        upsert_alias(db, company_id=company_id, customer_id=customer.id, alias=alias)
    for domain in iter_values(row.get("domains")):
        upsert_domain(db, company_id=company_id, customer_id=customer.id, domain=domain)
    if row.get("primary_email"):
        upsert_contact_point(db, company_id=company_id, customer_id=customer.id, type_="email", value=row["primary_email"], label="principal", contact_name=row.get("contact_name"), contact_role=row.get("contact_role"), is_primary=True)
    if row.get("phone"):
        upsert_contact_point(db, company_id=company_id, customer_id=customer.id, type_="phone", value=row["phone"], label="principal", contact_name=row.get("contact_name"), contact_role=row.get("contact_role"), is_primary=True)
    for email in iter_values(row.get("associated_emails")):
        upsert_contact_point(db, company_id=company_id, customer_id=customer.id, type_="email", value=email, label="asociado", contact_name=row.get("contact_name"), contact_role=row.get("contact_role"))
    for phone in iter_values(row.get("associated_phones")):
        upsert_contact_point(db, company_id=company_id, customer_id=customer.id, type_="phone", value=phone, label="asociado", contact_name=row.get("contact_name"), contact_role=row.get("contact_role"))

    return customer, "created" if created else "updated"


def seed_dialma_clients(db, *, company_name: str, admin_email: str, admin_password: str) -> dict[str, int]:
    company, company_created = ensure_company(db, company_name, admin_email)
    ensure_admin(db, company, admin_email, admin_password)

    created = updated = omitted = 0
    for row in CUSTOMERS:
        existing = find_customer(db, company.id, row)
        before_state = None
        if existing:
            before_state = {
                "code": existing.code,
                "fiscal_name": existing.fiscal_name,
                "commercial_name": existing.commercial_name or "",
                "primary_email": existing.primary_email or "",
                "phone": existing.phone or "",
                "status": existing.status,
                "company_inactive": bool(existing.company_inactive),
                "notes": existing.notes or "",
            }
        customer, state = apply_customer(db, company_id=company.id, row=row)
        db.flush()
        after_state = {
            "code": customer.code,
            "fiscal_name": customer.fiscal_name,
            "commercial_name": customer.commercial_name or "",
            "primary_email": customer.primary_email or "",
            "phone": customer.phone or "",
            "status": customer.status,
            "company_inactive": bool(customer.company_inactive),
            "notes": customer.notes or "",
        }
        if state == "created":
            created += 1
        elif before_state == after_state:
            omitted += 1
        else:
            updated += 1
    db.commit()
    return {"company_created": int(company_created), "created": created, "updated": updated, "omitted": omitted, "company_id": company.id}


def main() -> int:
    parser = argparse.ArgumentParser(description="Crea clientes de prueba para la organizacion Dialma.")
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD)
    parser.add_argument("--company-name", default=DEFAULT_COMPANY_NAME)
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        result = seed_dialma_clients(db, company_name=args.company_name, admin_email=args.admin_email, admin_password=args.admin_password)
        print("Clientes Dialma creados/actualizados:")
        print(f"- nuevos: {result['created']}")
        print(f"- actualizados: {result['updated']}")
        print(f"- omitidos: {result['omitted']}")
        print(f"company_id={result['company_id']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
