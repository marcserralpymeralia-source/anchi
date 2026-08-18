from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret
from app.db.models import BrandingSettings, Company, Customer, CustomerProductKnowledge, EmailSettings, InputChannel, LLMSettings, Product
from app.settings.email_config import email_config_status
from app.settings.service import get_or_create_settings
from app.whatsapp.service import whatsapp_config


SETUP_STEPS = (
    ("company", "Empresa"),
    ("channels", "Canales"),
    ("products", "Productos"),
    ("customers", "Clientes"),
    ("customer_knowledge", "Información adicional"),
    ("openai", "OpenAI"),
)


@dataclass(slots=True)
class SetupStatus:
    company_configured: bool
    email_connected: bool
    whatsapp_connected: bool
    has_input_channel: bool
    has_products: bool
    has_customers: bool
    openai_connected: bool
    optional_customer_knowledge: bool
    is_operational: bool
    current_step: str
    progress_percent: int
    counts: dict[str, int]
    steps: list[dict[str, Any]]


def _company_configured(db: Session, company_id: int) -> bool:
    company = db.get(Company, company_id)
    branding = db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == company_id))
    has_company = bool(company and (company.name or company.legal_name) and company.country and company.language and company.timezone)
    has_branding = bool(branding and branding.company_name and branding.app_name)
    return has_company and has_branding


def _openai_connected(settings: LLMSettings) -> bool:
    if settings.provider not in {"openai", "openai_compatible", "azure_openai"}:
        return False
    return bool(decrypt_secret(settings.api_key_encrypted))


def _step_status(key: str, completed: bool, current_step: str, *, optional: bool = False, error: bool = False) -> str:
    if optional and not completed:
        return "Opcional"
    if error:
        return "Error"
    if completed:
        return "Completado"
    if key == current_step:
        return "En progreso"
    return "Pendiente"


def get_setup_status(db: Session, company_id: int) -> SetupStatus:
    email = get_or_create_settings(db, EmailSettings, company_id)
    llm = get_or_create_settings(db, LLMSettings, company_id)
    email_status = email_config_status(email)
    whatsapp = whatsapp_config(db, company_id)
    product_count = db.scalar(select(func.count(Product.id)).where(Product.company_id == company_id, Product.deleted_at.is_(None))) or 0
    customer_count = db.scalar(select(func.count(Customer.id)).where(Customer.company_id == company_id, Customer.deleted_at.is_(None))) or 0
    knowledge_count = db.scalar(select(func.count(CustomerProductKnowledge.id)).where(CustomerProductKnowledge.company_id == company_id)) or 0
    active_channels_count = db.scalar(select(func.count(InputChannel.id)).where(InputChannel.company_id == company_id, InputChannel.is_active.is_(True))) or 0
    company_ready = _company_configured(db, company_id)
    email_connected = bool(email_status.get("imap_ready"))
    whatsapp_connected = bool(whatsapp.enabled and whatsapp.webhook_enabled and whatsapp.phone_number_id and whatsapp.verify_token and whatsapp.app_secret and whatsapp.access_token)
    has_input_channel = email_connected or whatsapp_connected or bool(active_channels_count)
    has_products = product_count > 0
    has_customers = customer_count > 0
    openai_connected = _openai_connected(llm)
    optional_customer_knowledge = knowledge_count > 0
    is_operational = company_ready and has_input_channel and has_products and has_customers and openai_connected

    if not company_ready:
        current_step = "company"
    elif not has_input_channel:
        current_step = "channels"
    elif not has_products:
        current_step = "products"
    elif not has_customers:
        current_step = "customers"
    elif not openai_connected:
        current_step = "openai"
    else:
        current_step = "complete"

    steps = [
        {"key": "company", "label": "Empresa", "status": _step_status("company", company_ready, current_step)},
        {"key": "channels", "label": "Canales", "status": _step_status("channels", has_input_channel, current_step)},
        {"key": "products", "label": "Productos", "status": _step_status("products", has_products, current_step)},
        {"key": "customers", "label": "Clientes", "status": _step_status("customers", has_customers, current_step)},
        {"key": "customer_knowledge", "label": "Información adicional", "status": _step_status("customer_knowledge", optional_customer_knowledge, current_step, optional=True)},
        {"key": "openai", "label": "OpenAI", "status": _step_status("openai", openai_connected, current_step)},
    ]
    completed_steps = len([step for step in steps if step["status"] == "Completado"])
    progress_percent = round((completed_steps * 100) / len(steps)) if steps else 0
    return SetupStatus(
        company_configured=company_ready,
        email_connected=email_connected,
        whatsapp_connected=whatsapp_connected,
        has_input_channel=has_input_channel,
        has_products=has_products,
        has_customers=has_customers,
        openai_connected=openai_connected,
        optional_customer_knowledge=optional_customer_knowledge,
        is_operational=is_operational,
        current_step=current_step,
        progress_percent=progress_percent,
        counts={"products": int(product_count), "customers": int(customer_count), "customer_knowledge": int(knowledge_count)},
        steps=steps,
    )


def next_setup_url(status: SetupStatus) -> str:
    return "/inicio" if status.is_operational else f"/setup/{status.current_step}"
