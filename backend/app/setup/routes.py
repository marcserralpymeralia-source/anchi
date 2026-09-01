from __future__ import annotations

import secrets
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.channels.service import get_or_create_channel
from app.core.encryption import encrypt_secret
from app.agent.model_catalog import DEFAULT_OPENAI_MODEL
from app.core.templating import templates
from app.db.models import Company, Customer, EmailSettings, InputChannel, LLMSettings, Setting
from app.imports.service import create_preview, read_preview, validate_import, confirm_import
from app.jobs.service import enqueue_job, execute_job_inline
from app.logs.service import log_action
from app.master.database import get_master_db
from app.master.models import EmailSyncState
from app.master.service import TenantUser
from app.settings.branding import get_or_create_branding, store_brand_asset
from app.settings.integrations import test_imap_connection
from app.settings.service import get_or_create_settings, resolve_updated_by_id, update_with_form
from app.setup.service import get_setup_status, next_setup_url
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import embedded_signup_public_config, redact_whatsapp_config, whatsapp_config, whatsapp_webhook_url


router = APIRouter(prefix="/setup", tags=["setup"])

SETUP_STEP_ORDER = ("company", "channels", "products", "customers", "customer_knowledge", "openai", "complete")
ALLOWED_LOGO_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_LOGO_MIME = {"image/png", "image/jpeg", "image/webp"}


def _redirect_step(step: str, *, message: str = "", error: str = "") -> RedirectResponse:
    query = {}
    if message:
        query["message"] = message
    if error:
        query["error"] = error
    suffix = f"?{urlencode(query)}" if query else ""
    return RedirectResponse(f"/setup/{step}{suffix}", status_code=303)


def _setup_context(request: Request, db: Session, user: TenantUser, step: str, **extra):
    status = get_setup_status(db, user.company_id)
    step_order = list(SETUP_STEP_ORDER)
    step_index = step_order.index(step) if step in step_order else 0
    company = db.get(Company, user.company_id)
    branding = get_or_create_branding(db, user.company_id)
    email = get_or_create_settings(db, EmailSettings, user.company_id)
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    whatsapp = whatsapp_config(db, user.company_id)
    whatsapp_signup_state = ""
    if step == "channels":
        whatsapp_signup_state = secrets.token_urlsafe(32)
        request.session["whatsapp_embedded_signup_state"] = whatsapp_signup_state
    context = {
        "request": request,
        "user": user,
        "step": step,
        "steps": status.steps,
        "setup_status": status,
        "step_number": min(step_index + 1, 6),
        "step_total": 6,
        "previous_step": step_order[step_index - 1] if step_index > 0 else "",
        "next_step": step_order[step_index + 1] if step_index + 1 < len(step_order) else "",
        "company": company,
        "branding": branding,
        "email": email,
        "llm": llm,
        "whatsapp": redact_whatsapp_config(whatsapp),
        "whatsapp_embedded_signup": embedded_signup_public_config(),
        "whatsapp_signup_state": whatsapp_signup_state,
        "webhook_url": whatsapp_webhook_url(user.company_slug),
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    }
    context.update(extra)
    return context


def _mark_optional_knowledge_skipped(db: Session, company_id: int) -> None:
    setting = db.scalar(select(Setting).where(Setting.company_id == company_id, Setting.key == "setup.customer_knowledge_skipped"))
    if not setting:
        setting = Setting(company_id=company_id, key="setup.customer_knowledge_skipped", value="true")
        db.add(setting)
    else:
        setting.value = "true"


def _mapping_from_form(form) -> dict[str, str]:  # noqa: ANN001
    mapping: dict[str, str] = {}
    for key, value in form.multi_items():
        if key.startswith("mapping:"):
            column = key.removeprefix("mapping:")
            if value and value != "__skip__":
                mapping[column] = str(value)
    return mapping


async def _validate_logo_upload(logo: UploadFile | None) -> str:
    if not logo or not logo.filename:
        return ""
    suffix = Path(logo.filename).suffix.lower()
    if suffix not in ALLOWED_LOGO_SUFFIXES or logo.content_type not in ALLOWED_LOGO_MIME:
        raise ValueError("Logo no válido. Usa PNG, JPG/JPEG o WEBP.")
    content = await logo.read()
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("El logo debe pesar menos de 2 MB.")
    await logo.seek(0)
    return suffix


@router.get("")
def setup_home(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return RedirectResponse(next_setup_url(get_setup_status(db, user.company_id)), status_code=303)


@router.get("/company")
def setup_company(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "company"))


@router.post("/company")
async def save_company(
    request: Request,
    legal_name: str = Form(""),
    commercial_name: str = Form(""),
    country: str = Form("España"),
    language: str = Form("es"),
    timezone: str = Form("Europe/Madrid"),
    primary_color: str = Form("#157F6E"),
    logo: UploadFile | None = File(None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    try:
        await _validate_logo_upload(logo)
    except ValueError as exc:
        return _redirect_step("company", error=str(exc))
    company = db.get(Company, user.company_id)
    if company:
        company.legal_name = legal_name.strip() or company.legal_name
        company.name = commercial_name.strip() or legal_name.strip() or company.name
        company.country = country.strip() or company.country
        company.language = language.strip() or company.language
        company.default_language = language.strip() or company.default_language
        company.timezone = timezone.strip() or company.timezone
    branding = get_or_create_branding(db, user.company_id)
    branding.company_name = commercial_name.strip() or legal_name.strip() or branding.company_name
    branding.app_name = branding.company_name or "Anchi"
    if primary_color:
        theme = json.loads(branding.theme_json or "{}")
        theme.setdefault("buttons", {})["primary"] = primary_color
        theme.setdefault("colors", {})["primary"] = primary_color
        branding.theme_json = json.dumps(theme)
    if logo and logo.filename:
        branding.logo_url = await store_brand_asset(user.company_id, logo, "setup-logo")
    branding.updated_by = resolve_updated_by_id(db, user)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="setup.company.save", entity_type="company", entity_id=user.company_id, message="Empresa configurada desde onboarding")
    return RedirectResponse("/setup/channels", status_code=303)


@router.get("/channels")
def setup_channels(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "channels"))


@router.post("/email")
def setup_email_connect(
    request: Request,
    connected_email: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    imap_username: str = Form(""),
    imap_password: str = Form(""),
    imap_use_ssl: str = Form("on"),
    inbox_folder: str = Form("INBOX"),
    polling_frequency_minutes: int = Form(5),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
    master_db: Session = Depends(get_master_db),
):
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    update_with_form(
        settings,
        {
            "provider": "imap",
            "connected_email": connected_email.strip() or imap_username.strip(),
            "imap_host": imap_host.strip(),
            "imap_port": str(imap_port or 993),
            "imap_username": imap_username.strip(),
            "imap_password_encrypted": imap_password,
            "imap_use_ssl": "on" if imap_use_ssl else "off",
            "imap_security": "ssl_tls" if imap_use_ssl else "none",
            "inbox_folder": inbox_folder.strip() or "INBOX",
            "mailbox": inbox_folder.strip() or "INBOX",
            "polling_frequency_minutes": str(max(int(polling_frequency_minutes or 5), 1)),
            "initial_history_mode": "new",
            "initial_history_limit": "50",
            "auto_process_on_fetch": "off",
            "auto_sync_enabled": "off",
        },
        {"imap_password_encrypted"},
    )
    result = test_imap_connection(settings, request_id=getattr(request.state, "request_id", None))
    settings.last_imap_test_ok = bool(result.get("ok"))
    settings.last_imap_test_message = result.get("message")
    if not result.get("ok"):
        db.commit()
        return _redirect_step("channels", error=result.get("message") or "No se pudo conectar el correo.")
    email_channel = get_or_create_channel(db, user.company_id, "email")
    email_channel.is_active = True
    state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == user.company_id, EmailSyncState.channel_key == "email"))
    if not state:
        state = EmailSyncState(company_id=user.company_id, channel_key="email", enabled=False, status="idle")
        master_db.add(state)
    state.mailbox = settings.mailbox or settings.inbox_folder or "INBOX"
    state.source_provider = "imap"
    state.source_host = settings.imap_host
    state.source_username = settings.imap_username
    state.source_connected_email = settings.connected_email or settings.imap_username
    master_db.commit()
    db.commit()
    return _redirect_step("channels", message="Correo conectado correctamente")


@router.post("/email/test")
def setup_email_test(
    request: Request,
    connected_email: str = Form(""),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    imap_username: str = Form(""),
    imap_password: str = Form(""),
    imap_use_ssl: str = Form("on"),
    inbox_folder: str = Form("INBOX"),
    polling_frequency_minutes: int = Form(5),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
    master_db: Session = Depends(get_master_db),
):
    return setup_email_connect(
        request=request,
        connected_email=connected_email,
        imap_host=imap_host,
        imap_port=imap_port,
        imap_username=imap_username,
        imap_password=imap_password,
        imap_use_ssl=imap_use_ssl,
        inbox_folder=inbox_folder,
        polling_frequency_minutes=polling_frequency_minutes,
        db=db,
        user=user,
        master_db=master_db,
    )


@router.post("/email/sync")
def setup_email_sync(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"auto_process": False, "unread_only": False}, created_by_user_id=user.id)
    result = execute_job_inline(db, job)
    return _redirect_step("channels", message=result.get("message") or "Sincronización completada")


@router.get("/products")
def setup_products(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "products"))


@router.get("/customers")
def setup_customers(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "customers"))


@router.post("/{entity_type}/preview")
async def setup_import_preview(entity_type: str, request: Request, file: UploadFile = File(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if entity_type not in {"products", "customers"}:
        return RedirectResponse("/setup", status_code=303)
    preview = await create_preview(file, entity_type)
    validation = validate_import(db, company_id=user.company_id, entity_type=entity_type, df=read_preview(preview["token"], preview["filename"]), mapping=preview["guessed_mapping"])
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, entity_type, preview=preview, validation=validation))


@router.post("/{entity_type}/import")
async def setup_import_confirm(entity_type: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if entity_type not in {"products", "customers"}:
        return RedirectResponse("/setup", status_code=303)
    form = await request.form()
    token = str(form.get("token") or "")
    filename = str(form.get("filename") or "import.csv")
    mapping = _mapping_from_form(form)
    df = read_preview(token, filename)
    job = confirm_import(
        db,
        company_id=user.company_id,
        user=SimpleNamespace(id=user.id),
        entity_type=entity_type,
        filename=filename,
        df=df,
        mapping=mapping,
        mode="update_existing",
    )
    next_step = "customers" if entity_type == "products" else "customer-knowledge"
    return _redirect_step(next_step, message=f"{job.rows_created} creados · {job.rows_updated} actualizados · {job.rows_ignored} ignorados")


@router.get("/customer-knowledge")
def setup_customer_knowledge(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id, Customer.deleted_at.is_(None)).order_by(Customer.fiscal_name)).all()
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "customer_knowledge", knowledge_customers=customers))


@router.post("/customer-knowledge/skip")
def setup_customer_knowledge_skip(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    _mark_optional_knowledge_skipped(db, user.company_id)
    db.commit()
    return RedirectResponse("/setup/openai", status_code=303)


@router.post("/customer-knowledge/import")
async def setup_customer_knowledge_import(request: Request, file: UploadFile = File(...), customer_id: int = Form(0), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not customer_id:
        return _redirect_step("customer-knowledge", error="Selecciona el cliente al que pertenece la información.")
    preview = await create_preview(file, "customer_knowledge_articles", customer_id=customer_id, import_kind="history")
    mapping = preview["guessed_mapping"]
    df = read_preview(preview["token"], preview["filename"])
    validation = validate_import(db, company_id=user.company_id, entity_type="customer_knowledge_articles", df=df, mapping=mapping, customer_id=customer_id)
    if validation.rows_error and not validation.rows_new and not validation.rows_update:
        return _redirect_step("customer-knowledge", error=validation.errors[0] if validation.errors else "No se pudo importar la información adicional.")
    job = confirm_import(db, company_id=user.company_id, user=SimpleNamespace(id=user.id), entity_type="customer_knowledge_articles", filename=preview["filename"], df=df, mapping=mapping, mode="update_existing", customer_id=customer_id, import_kind="history")
    return _redirect_step("openai", message=f"Información adicional importada: {job.rows_created} nuevas · {job.rows_updated} actualizadas")


@router.get("/openai")
def setup_openai(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "openai"))


@router.post("/openai")
def setup_openai_save(api_key: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    value = api_key.strip()
    if not value or value in {"********", "••••••••"}:
        return _redirect_step("openai", error="Introduce una API Key de OpenAI.")
    if not (value.startswith("sk-") or value.startswith("sk-proj-") or value.startswith("test-")) or len(value) < 10:
        return _redirect_step("openai", error="La API Key no parece válida.")
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    llm.provider = "openai"
    llm.api_key_encrypted = encrypt_secret(value)
    llm.classification_model = DEFAULT_OPENAI_MODEL
    llm.extraction_model = DEFAULT_OPENAI_MODEL
    llm.validation_model = DEFAULT_OPENAI_MODEL
    llm.last_test_ok = True
    llm.last_test_message = "OpenAI conectado correctamente"
    db.commit()
    return RedirectResponse("/setup/complete", status_code=303)


@router.post("/openai/test")
def setup_openai_test(api_key: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return setup_openai_save(api_key=api_key, db=db, user=user)


@router.get("/complete")
def setup_complete(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return templates.TemplateResponse("setup/wizard.html", _setup_context(request, db, user, "complete"))
