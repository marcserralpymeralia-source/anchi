#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"

import sys

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.core.security import hash_password
from app.db.database import Base, ensure_schema_for_engine
from app.db.models import (
    BrandingSettings,
    Company,
    DecisionSettings,
    EmailSettings,
    ExportSettings,
    FTPSettings,
    InputChannel,
    LLMSettings,
    PromptTemplate,
    PromptVersion,
    Role,
    ScoringSettings,
    User,
)
from app.master.database import MasterSessionLocal
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.master.provisioning import tenant_database_path
from app.master.service import slugify
from app.settings.branding import reset_branding
from app.tenancy.database import ensure_tenant_schema


DEFAULT_PROMPTS = {
    "classification": "Clasifica el correo como pedido, no_pedido, consulta, incidencia o dudoso. Responde JSON valido con tipo_correo, confianza y motivo.",
    "extraction": "Extrae un pedido en JSON valido con cliente, fechas, observaciones y lineas con producto, referencia, cantidad y unidad.",
    "validation": "Valida el pedido extraido contra datos de cliente y producto. Devuelve JSON con advertencias y bloqueos.",
    "non_order": "Resume por que el correo no contiene pedido y clasificalo como consulta, incidencia, no_pedido o dudoso.",
}


def _tenant_session(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    ensure_schema_for_engine(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _ensure_master_company(master_db: Session, name: str, slug: str) -> MasterCompany:
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == slug))
    if company:
        company.name = name
        company.legal_name = company.legal_name or name
        company.active = True
        return company
    company = MasterCompany(name=name, slug=slug, legal_name=name, active=True)
    master_db.add(company)
    master_db.flush()
    return company


def _ensure_master_user(master_db: Session, email: str, full_name: str, password: str) -> MasterUser:
    user = master_db.scalar(select(MasterUser).where(MasterUser.email == email))
    if not user:
        user = MasterUser(email=email, full_name=full_name, password_hash=hash_password(password), is_active=True)
        master_db.add(user)
        master_db.flush()
        return user
    user.full_name = full_name
    user.password_hash = hash_password(password)
    user.is_active = True
    master_db.flush()
    return user


def _ensure_membership(master_db: Session, user: MasterUser, company: MasterCompany) -> CompanyMembership:
    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company.id,
        )
    )
    if not membership:
        membership = CompanyMembership(user_id=user.id, company_id=company.id, role_key="Administrador", is_active=True, is_owner=True)
        master_db.add(membership)
        master_db.flush()
    else:
        membership.role_key = "Administrador"
        membership.is_active = True
        membership.is_owner = True
    return membership


def _ensure_tenant_master_link(master_db: Session, company: MasterCompany) -> MasterTenantDatabase:
    tenant_url = f"sqlite:///{tenant_database_path(company).as_posix()}"
    tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    if not tenant:
        tenant = MasterTenantDatabase(
            company_id=company.id,
            database_key=slugify(company.slug or company.name),
            database_url=tenant_url,
            database_type="sqlite",
            is_active=True,
            health_status="pending",
        )
        master_db.add(tenant)
        master_db.flush()
    else:
        tenant.database_key = slugify(company.slug or company.name)
        tenant.database_url = tenant_url
        tenant.database_type = "sqlite"
        tenant.is_active = True
    return tenant


def _ensure_tenant_company(db: Session, master_company: MasterCompany) -> Company:
    company = db.get(Company, master_company.id)
    if not company:
        company = Company(id=master_company.id, name=master_company.name, legal_name=master_company.legal_name or master_company.name, active=True, plan="client", currency="EUR", language="es", default_language="es", timezone="Europe/Madrid", date_format="%d/%m/%Y", decimal_separator=",")
        db.add(company)
        db.flush()
    else:
        company.name = master_company.name
        company.legal_name = master_company.legal_name or master_company.name
        company.active = True
    return company


def _ensure_role(db: Session, company_id: int, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.company_id == company_id, Role.name == name))
    if role:
        role.permissions = role.permissions or DEFAULT_ROLE_PERMISSIONS.get(name, "")
        return role
    role = Role(company_id=company_id, name=name, permissions=DEFAULT_ROLE_PERMISSIONS.get(name, ""))
    db.add(role)
    db.flush()
    return role


def _ensure_tenant_user(db: Session, company_id: int, role: Role, email: str, full_name: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(company_id=company_id, role_id=role.id, email=email, name=full_name, password_hash=hash_password(password), is_active=True)
        db.add(user)
        db.flush()
        return user
    user.company_id = company_id
    user.role_id = role.id
    user.name = full_name
    user.password_hash = hash_password(password)
    user.is_active = True
    db.flush()
    return user


def _ensure_branding(db: Session, company_id: int, user_id: int, company_name: str) -> None:
    branding = db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == company_id))
    if not branding:
        branding = BrandingSettings(company_id=company_id)
        db.add(branding)
        db.flush()
    reset_branding(branding, user_id)
    branding.company_name = company_name
    branding.app_name = "Anchi"
    branding.primary_claim = "Gestion inteligente de pedidos"
    branding.short_description = "Plataforma demo para la revision, validacion y exportacion de pedidos."
    branding.logo_url = None
    branding.dark_logo_url = None
    branding.favicon_url = None


def _ensure_input_channels(db: Session, company_id: int) -> None:
    definitions = [
        {"key": "email", "name": "Email", "is_active": True, "is_default": True, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": True, "supports_images": False},
        {"key": "whatsapp", "name": "WhatsApp", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": True, "supports_documents": False, "supports_images": True},
        {"key": "voice", "name": "Teléfono / voz", "is_active": False, "is_default": False, "supports_text": False, "supports_attachments": False, "supports_audio": True, "supports_documents": False, "supports_images": False},
        {"key": "social", "name": "Redes sociales", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": False, "supports_images": True},
    ]
    for definition in definitions:
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.key == definition["key"]))
        if not channel:
            db.add(InputChannel(company_id=company_id, channel_type="message", **definition))
        else:
            for key, value in definition.items():
                setattr(channel, key, value)


def _ensure_settings(db: Session, company_id: int, user_id: int) -> None:
    email = db.scalar(select(EmailSettings).where(EmailSettings.company_id == company_id)) or EmailSettings(company_id=company_id)
    email.provider = "imap"
    email.connection_method = "password"
    email.inbox_folder = "INBOX"
    email.read_limit = 25
    email.test_read_limit = 10
    email.auto_sync_enabled = False
    email.read_unread_only = True
    email.mark_as_read_after_import = False
    email.move_after_processing = False
    email.post_process_action = "mark_read"
    email.polling_frequency_minutes = 15
    email.save_internal_copy = True
    email.preserve_thread_headers = True
    email.auto_process_on_fetch = False
    email.process_without_attachments = True
    email.avoid_duplicates_by_message_id = True
    email.allow_reprocess = False
    email.auto_create_order_if_detected = True
    email.always_human_review = True
    email.mark_doubtful_below_threshold = True
    email.mark_no_order_if_detected = True
    email.minimum_score_auto_order = 90
    email.signature_text = "Equipo Anchi"
    db.add(email)

    llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id)) or LLMSettings(company_id=company_id)
    llm.agent_enabled = True
    llm.agent_mode = "semiautomatico"
    llm.safety_level = "equilibrado"
    llm.provider = "openai"
    llm.classification_model = "gpt-4.1-mini"
    llm.extraction_model = "gpt-4.1-mini"
    llm.validation_model = "gpt-4.1-mini"
    llm.use_same_model_for_all = True
    llm.can_read_email = True
    llm.can_extract_pdf = True
    llm.can_classify_email = True
    llm.can_extract_order = True
    llm.can_suggest_customer = True
    llm.can_suggest_products = True
    llm.can_calculate_score = True
    llm.allow_auto_confirm = False
    llm.allow_auto_export = False
    llm.debug_mode = True
    db.add(llm)

    scoring = db.scalar(select(ScoringSettings).where(ScoringSettings.company_id == company_id)) or ScoringSettings(company_id=company_id)
    scoring.safe_threshold = 90
    scoring.review_threshold = 75
    scoring.doubtful_threshold = 50
    scoring.blocked_threshold = 49
    scoring.customer_weight = 25
    scoring.products_weight = 40
    scoring.quantities_weight = 20
    scoring.coherence_weight = 10
    scoring.llm_weight = 5
    scoring.block_without_customer = True
    scoring.block_without_reference = True
    scoring.block_without_quantity = True
    scoring.block_below_threshold = True
    db.add(scoring)

    decision = db.scalar(select(DecisionSettings).where(DecisionSettings.company_id == company_id)) or DecisionSettings(company_id=company_id)
    decision.enable_exact_match = True
    decision.enable_alias_match = True
    decision.enable_relation_match = True
    decision.enable_history_match = True
    decision.enable_rag_match = True
    decision.enable_llm_support = True
    decision.always_human_review = True
    decision.learning_mode = "supervisado"
    decision.block_conflicting_aliases = True
    decision.block_missing_quantity = True
    decision.block_missing_reference = True
    db.add(decision)

    export = db.scalar(select(ExportSettings).where(ExportSettings.company_id == company_id)) or ExportSettings(company_id=company_id)
    export.file_type = "csv"
    export.csv_separator = ";"
    export.encoding = "utf-8"
    export.include_header = True
    export.filename_template = "PEDIDO_{codigo_cliente}_{fecha}_{id_pedido}.csv"
    db.add(export)

    ftp = db.scalar(select(FTPSettings).where(FTPSettings.company_id == company_id)) or FTPSettings(company_id=company_id)
    ftp.connection_type = "sftp"
    ftp.destination_path = f"/{slugify(str(company_id))}"
    ftp.passive_mode = True
    ftp.overwrite_files = False
    ftp.retries = 2
    ftp.timeout_seconds = 30
    db.add(ftp)

    for purpose, content in DEFAULT_PROMPTS.items():
        template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
        if not template:
            template = PromptTemplate(company_id=company_id, name=purpose.replace("_", " ").title(), purpose=purpose)
            db.add(template)
            db.flush()
        version = db.scalar(select(PromptVersion).where(PromptVersion.template_id == template.id, PromptVersion.version == 1))
        if not version:
            version = PromptVersion(company_id=company_id, template_id=template.id, version=1, content=content, created_by_user_id=user_id)
            db.add(version)
            db.flush()
        template.active_version_id = version.id


def provision_company(name: str, slug: str, admin_email: str, admin_password: str, *, force: bool = False) -> dict[str, str]:
    master_db = MasterSessionLocal()
    try:
        company = _ensure_master_company(master_db, name, slug)
        user = _ensure_master_user(master_db, admin_email, f"Administrador {name}", admin_password)
        membership = _ensure_membership(master_db, user, company)
        tenant = _ensure_tenant_master_link(master_db, company)
        master_db.commit()

        tenant_db_path = tenant_database_path(company)
        tenant_db_path.parent.mkdir(parents=True, exist_ok=True)
        if tenant_db_path.exists() and not force:
            raise FileExistsError(f"Ya existe la base tenant: {tenant_db_path}")
        engine = create_engine(tenant.database_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        ensure_tenant_schema(tenant.database_url)
        SessionFactory = _tenant_session(tenant.database_url)
        tenant_db = SessionFactory()
        try:
            tenant_company = _ensure_tenant_company(tenant_db, company)
            admin_role = _ensure_role(tenant_db, tenant_company.id, "Administrador")
            _ensure_role(tenant_db, tenant_company.id, "Superadmin")
            _ensure_role(tenant_db, tenant_company.id, "Supervisor")
            _ensure_role(tenant_db, tenant_company.id, "Operador")
            _ensure_role(tenant_db, tenant_company.id, "Solo lectura")
            tenant_user = _ensure_tenant_user(tenant_db, tenant_company.id, admin_role, admin_email, f"Administrador {name}", admin_password)
            _ensure_branding(tenant_db, tenant_company.id, tenant_user.id, name)
            _ensure_input_channels(tenant_db, tenant_company.id)
            _ensure_settings(tenant_db, tenant_company.id, tenant_user.id)
            tenant_db.commit()
        finally:
            tenant_db.close()

        tenant.health_status = "ok"
        tenant.notes = f"Tenant provisioned at {tenant_db_path.as_posix()}"
        master_db.commit()
        return {
            "company_id": str(company.id),
            "company_slug": company.slug,
            "tenant_database": tenant.database_url,
            "admin_email": admin_email,
            "admin_password": admin_password,
            "membership_id": str(membership.id),
            "login_url": "http://127.0.0.1:8001/login",
        }
    finally:
        master_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Provisiona una nueva compania en master + tenant.")
    parser.add_argument("--name", required=True, help="Nombre de la empresa")
    parser.add_argument("--slug", help="Slug tecnico; si se omite, se genera desde el nombre")
    parser.add_argument("--admin-email", required=True, help="Email del admin")
    parser.add_argument("--admin-password", required=True, help="Password del admin")
    parser.add_argument("--force", action="store_true", help="Permite sobrescribir la base tenant si ya existe")
    args = parser.parse_args()

    slug = slugify(args.slug or args.name)
    result = provision_company(args.name, slug, args.admin_email, args.admin_password, force=args.force)
    print("Compañía provisionada:")
    for key, value in result.items():
        print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
