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
from app.db.database import Base
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


DEFAULT_ADMIN_PASSWORD = "DialmaDemo2026!"
DEFAULT_ADMIN_EMAIL = "admin@dialma.local"


def _copy_columns(source, *, exclude: set[str] | None = None) -> dict:
    exclude = exclude or set()
    data: dict = {}
    for column in source.__table__.columns:
        if column.name in exclude:
            continue
        data[column.name] = getattr(source, column.name)
    return data


def _create_target_session(target_db: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{target_db.as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _source_company(db: Session, company_name: str | None) -> Company:
    stmt = select(Company).order_by(Company.id.asc())
    if company_name:
        stmt = select(Company).where(Company.name == company_name).order_by(Company.id.asc())
    company = db.scalars(stmt).first()
    if not company:
        raise RuntimeError("No se ha encontrado una empresa origen para clonar.")
    return company


def _seed_roles(source_db: Session, target_db: Session, source_company: Company, target_company: Company) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    source_roles = source_db.scalars(select(Role).where(Role.company_id == source_company.id).order_by(Role.id.asc())).all()
    if not source_roles:
        source_roles = [Role(company_id=source_company.id, name=name, permissions=DEFAULT_ROLE_PERMISSIONS.get(name, "")) for name in ("Administrador", "Superadmin", "Supervisor", "Operador", "Solo lectura")]
    for source_role in source_roles:
        role = Role(
            company_id=target_company.id,
            name=source_role.name,
            permissions=source_role.permissions or DEFAULT_ROLE_PERMISSIONS.get(source_role.name, ""),
        )
        target_db.add(role)
        target_db.flush()
        roles[role.name] = role
    return roles


def _seed_branding(source_db: Session, target_db: Session, source_company: Company, target_company: Company, user_id: int) -> None:
    source_branding = source_db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == source_company.id))
    if source_branding:
        data = _copy_columns(source_branding, exclude={"id", "company_id", "updated_by"})
        data["company_id"] = target_company.id
        data["company_name"] = target_company.name
        data["updated_by"] = user_id
        target_db.add(BrandingSettings(**data))
    else:
        target_db.add(BrandingSettings(company_id=target_company.id, company_name=target_company.name, app_name="Anchi", primary_claim="Gestion inteligente de pedidos"))


def _seed_email_settings(target_db: Session, target_company: Company) -> None:
    target_db.add(
        EmailSettings(
            company_id=target_company.id,
            provider="imap",
            connection_method="password",
            imap_use_ssl=True,
            imap_security="ssl_tls",
            inbox_folder="INBOX",
            read_limit=25,
            test_read_limit=10,
            auto_sync_enabled=False,
            read_unread_only=True,
            mark_as_read_after_import=False,
            move_after_processing=False,
            post_process_action="mark_read",
            polling_frequency_minutes=15,
            save_internal_copy=True,
            preserve_thread_headers=True,
            auto_process_on_fetch=False,
            process_without_attachments=True,
            avoid_duplicates_by_message_id=True,
            allow_reprocess=False,
            auto_create_order_if_detected=True,
            always_human_review=True,
            mark_doubtful_below_threshold=True,
            mark_no_order_if_detected=True,
            action_order_detected="move_processed",
            action_no_order="move_no_order",
            action_doubtful="move_doubtful",
            action_error="move_error",
            minimum_score_auto_order=90,
            default_filter="all",
            default_date_range="today",
            default_page_size=25,
            default_sort="date_desc",
            show_summary_cards=True,
            show_score_column=True,
            show_customer_column=True,
            show_attachments_column=True,
            show_order_column=True,
            show_reply_button=True,
            show_process_button=True,
            signature_text="Equipo de pedidos",
            use_signature=True,
        )
    )


def _seed_llm_settings(source_db: Session, target_db: Session, source_company: Company, target_company: Company, user_id: int) -> None:
    source_llm = source_db.scalar(select(LLMSettings).where(LLMSettings.company_id == source_company.id))
    if source_llm:
        data = _copy_columns(source_llm, exclude={"id", "company_id", "updated_by"})
        data["company_id"] = target_company.id
        data["updated_by"] = user_id
        target_db.add(LLMSettings(**data))
        return
    target_db.add(
        LLMSettings(
            company_id=target_company.id,
            provider="openai",
            agent_enabled=True,
            agent_mode="semiautomatico",
            safety_level="equilibrado",
            classification_model="gpt-4.1-mini",
            extraction_model="gpt-4.1-mini",
            validation_model="gpt-4.1-mini",
            use_same_model_for_all=True,
            can_read_email=True,
            can_extract_pdf=True,
            can_classify_email=True,
            can_extract_order=True,
            can_suggest_customer=True,
            can_suggest_products=True,
            can_calculate_score=True,
            can_create_pending_order=True,
            can_mark_no_order=True,
            allow_auto_confirm=False,
            allow_auto_export=False,
            temperature=0.1,
            max_tokens=4000,
            timeout_seconds=60,
            retries=2,
            batch_limit=25,
            detailed_llm_logs=False,
            store_llm_payloads=False,
            anonymize_llm_logs=True,
            debug_mode=False,
        )
    )


def _seed_settings(source_db: Session, target_db: Session, source_company: Company, target_company: Company) -> None:
    for model in (ScoringSettings, DecisionSettings, ExportSettings):
        source_row = source_db.scalar(select(model).where(model.company_id == source_company.id))
        if source_row:
            target_db.add(model(**_copy_columns(source_row, exclude={"id", "company_id"}), company_id=target_company.id))
    source_ftp = source_db.scalar(select(FTPSettings).where(FTPSettings.company_id == source_company.id))
    if source_ftp:
        data = _copy_columns(source_ftp, exclude={"id", "company_id", "host", "username", "password_encrypted", "private_key_encrypted"})
        data.update({"company_id": target_company.id, "host": None, "username": None, "password_encrypted": None, "private_key_encrypted": None})
        target_db.add(FTPSettings(**data))


def _seed_input_channels(target_db: Session, target_company: Company) -> None:
    definitions = [
        {"key": "email", "name": "Email", "is_active": True, "is_default": True, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": True, "supports_images": False},
        {"key": "whatsapp", "name": "WhatsApp", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": True, "supports_documents": False, "supports_images": True},
        {"key": "voice", "name": "Teléfono / voz", "is_active": False, "is_default": False, "supports_text": False, "supports_attachments": False, "supports_audio": True, "supports_documents": False, "supports_images": False},
        {"key": "social", "name": "Redes sociales", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": False, "supports_images": True},
    ]
    for definition in definitions:
        target_db.add(InputChannel(company_id=target_company.id, channel_type="message", **definition))


def _seed_prompts(source_db: Session, target_db: Session, source_company: Company, target_company: Company, user_id: int) -> None:
    templates = source_db.scalars(select(PromptTemplate).where(PromptTemplate.company_id == source_company.id).order_by(PromptTemplate.id.asc())).all()
    for template in templates:
        data = _copy_columns(template, exclude={"id", "company_id", "active_version_id"})
        data["company_id"] = target_company.id
        new_template = PromptTemplate(**data)
        target_db.add(new_template)
        target_db.flush()
        version = None
        if template.active_version_id:
            version = source_db.get(PromptVersion, template.active_version_id)
        if not version:
            version = source_db.scalar(
                select(PromptVersion)
                .where(PromptVersion.company_id == source_company.id, PromptVersion.template_id == template.id)
                .order_by(PromptVersion.version.desc())
            )
        if version:
            new_version = PromptVersion(
                company_id=target_company.id,
                template_id=new_template.id,
                version=version.version,
                content=version.content,
                created_by_user_id=user_id,
            )
            target_db.add(new_version)
            target_db.flush()
            new_template.active_version_id = new_version.id


def create_dialma_db(source_db_path: Path, target_db_path: Path, company_name: str, admin_email: str, admin_password: str) -> dict[str, str]:
    if not source_db_path.exists():
        raise FileNotFoundError(f"No existe la base de origen: {source_db_path}")
    if target_db_path.exists():
        raise FileExistsError(f"Ya existe la base destino: {target_db_path}")

    source_engine = create_engine(f"sqlite:///{source_db_path.as_posix()}")
    source_session_factory = sessionmaker(bind=source_engine, autoflush=False, autocommit=False)
    target_session_factory = _create_target_session(target_db_path)

    source_db = source_session_factory()
    target_db = target_session_factory()
    try:
        source_company = _source_company(source_db, None)
        target_company = Company(
            name=company_name,
            legal_name=company_name,
            active=True,
            plan=source_company.plan or "client",
            currency=source_company.currency or "EUR",
            language=source_company.language or "es",
            default_language=source_company.default_language or "es",
            timezone=source_company.timezone or "Europe/Madrid",
            date_format=source_company.date_format or "%d/%m/%Y",
            decimal_separator=source_company.decimal_separator or ",",
            email=admin_email,
        )
        target_db.add(target_company)
        target_db.flush()

        roles = _seed_roles(source_db, target_db, source_company, target_company)
        admin_role = roles.get("Administrador") or next(iter(roles.values()))
        admin = User(
            company_id=target_company.id,
            role_id=admin_role.id,
            email=admin_email,
            name="Administrador",
            password_hash=hash_password(admin_password),
            is_active=True,
        )
        target_db.add(admin)
        target_db.flush()

        _seed_branding(source_db, target_db, source_company, target_company, admin.id)
        _seed_email_settings(target_db, target_company)
        _seed_llm_settings(source_db, target_db, source_company, target_company, admin.id)
        _seed_settings(source_db, target_db, source_company, target_company)
        _seed_input_channels(target_db, target_company)
        _seed_prompts(source_db, target_db, source_company, target_company, admin.id)

        target_db.commit()
        return {
            "db_path": str(target_db_path),
            "company_id": str(target_company.id),
            "company_name": target_company.name,
            "admin_email": admin_email,
            "admin_password": admin_password,
        }
    except Exception:
        target_db.rollback()
        raise
    finally:
        source_db.close()
        target_db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Crea una bbdd nueva para un cliente a partir de la configuracion base.")
    parser.add_argument("--source-db", default=str(ROOT / "backend" / "gemavi.db"), help="Base SQLite de origen.")
    parser.add_argument("--target-db", default=str(ROOT / "backend" / "dialma.db"), help="Base SQLite destino.")
    parser.add_argument("--company-name", default="Dialma", help="Nombre de la empresa nueva.")
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL, help="Email del usuario administrador.")
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD, help="Password del usuario administrador.")
    args = parser.parse_args()

    result = create_dialma_db(Path(args.source_db).expanduser().resolve(), Path(args.target_db).expanduser().resolve(), args.company_name, args.admin_email, args.admin_password)
    print(f"BBDD creada: {result['db_path']}")
    print(f"Empresa: {result['company_name']}")
    print(f"Admin: {result['admin_email']}")
    print(f"Password: {result['admin_password']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
