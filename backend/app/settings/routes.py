import logging
import json
import re
from types import SimpleNamespace
from datetime import datetime, timezone
from datetime import date
from urllib.parse import urlsplit, urlunsplit
from time import perf_counter

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.core.config import get_settings
from app.core.middleware import invalidate_branding_cache
from app.auth.dependencies import current_user, require_tenant_role
from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, DEFAULT_REASONING_EFFORT, LEGACY_OPENAI_MODEL_FALLBACK, REASONING_EFFORT_VALUES, openai_model_description, openai_model_label, openai_model_option_payload, OPENAI_MODEL_PRESET_VALUES, reasoning_effort_option_payload, resolve_openai_model_choice, resolve_openai_runtime_model
from app.master.database import get_master_db
from app.master.service import TenantUser
from app.master.models import EmailSyncState
from app.core.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import AuditLog, BrandingSettings, Company, Customer, DecisionSettings, Email, EmailSettings, EmailTemplate, ExportSettings, FTPSettings, InputChannel, InboundMessage, LLMSettings, Order, Product, PromptExecution, PromptExecutionDetail, PromptTemplate, PromptVersion, ProxyConnection, ScoringSettings
from app.db.models import BackgroundJob
from app.logs.service import log_action
from app.settings.agent_config import agent_metrics, agent_status, apply_safety_level, improvement_suggestions
from app.settings.autoconfig import detect_email_configuration
from app.settings.branding import branding_to_dict, delete_brand_asset, get_or_create_branding, reset_branding, store_brand_asset, update_branding_from_form
from app.settings.email_config import TEMPLATE_VARIABLES, email_config_status, email_templates, ensure_default_email_templates, serialize_email_settings
from app.settings.integrations import AGENT_FLOW_DEMO_SAMPLE, AGENT_FLOW_DEMO_VALIDATION_CONTEXT, classify_sample, extract_sample, preview_initial_imap_sync, run_initial_imap_sync, send_test_email, test_imap_connection, test_smtp_connection, validate_sample
from app.settings.application import run_connection_test, update_settings_section_async
from app.settings.service import get_or_create_settings, resolve_updated_by_id, update_with_form
from app.dashboard.service import recent_processed_emails_overview
from app.jobs.service import enqueue_job, execute_job_inline, job_payload
from app.tenancy.database import get_tenant_db
from app.tenancy.migrations import tenant_migration_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

SETTINGS_MODULE_KEYS = {"general", "identity", "email", "ai", "scoring", "decision", "export", "ftp", "proxies", "advanced"}

SETTINGS_SEARCH_CATALOG = [
    {"module_key": "general", "module_label": "General", "title": "Datos de empresa", "detail": "Nombre, contacto, país, idioma y moneda", "search_text": "empresa nombre razón social CIF NIF email teléfono web dirección país idioma zona horaria moneda notificaciones"},
    {"module_key": "identity", "module_label": "Identidad", "title": "Marca y apariencia", "detail": "Logos, colores, sidebar y tipografía", "search_text": "identidad marca logo favicon color principal fondo radio botón sidebar claim tipografía apariencia"},
    {"module_key": "email", "module_label": "Correo", "title": "Cuenta y recepción", "detail": "IMAP, sincronización, histórico y carpetas", "search_text": "correo email IMAP cuenta recepción sincronización histórico carpeta servidor usuario contraseña /settings/email/receive"},
    {"module_key": "email", "module_label": "Correo", "title": "Envío SMTP", "detail": "Servidor, remitente y prueba de envío", "search_text": "SMTP envío servidor puerto seguridad remitente contraseña prueba correo"},
    {"module_key": "ai", "module_label": "Agente IA", "title": "Configuración del agente", "detail": "Proveedor, API key, modelos y capacidades", "search_text": "IA agente OpenAI proveedor API key modelo clasificación extracción validación seguridad capacidades prueba GPT-5.6 Luna GPT-5.6 Terra GPT-5.6 Sol GPT-4.1 mini GPT-4.1 Personalizado"},
    {"module_key": "scoring", "module_label": "Confianza y automatización", "title": "Umbrales y reglas", "detail": "Confianza, pesos, bloqueos y auto-confirmación", "search_text": "scoring confianza umbral pesos dudoso bloqueado revisión auto confirmar auto exportar"},
    {"module_key": "decision", "module_label": "Motor de decisión", "title": "Prioridades y aprendizaje", "detail": "Fuentes, pesos y bloqueos", "search_text": "decisión prioridad exacto alias histórico RAG LLM aprendizaje bloqueo"},
    {"module_key": "export", "module_label": "Exportación", "title": "Formato de exportación", "detail": "CSV, JSON, separadores y plantilla", "search_text": "exportación CSV JSON encoding fecha separador plantilla cabecera líneas"},
    {"module_key": "ftp", "module_label": "FTP/SFTP", "title": "Destino de exportación", "detail": "FTP, FTPS, host, credenciales y reintentos", "search_text": "FTP FTPS SFTP host puerto usuario contraseña clave privada destino reintentos timeout"},
    {"module_key": "proxies", "module_label": "Proxies", "title": "Acceso al gateway", "detail": "Perfiles de acceso para el futuro gateway de red", "search_text": "proxy proxies gateway conexión externa IP host puerto protocolo usuario contraseña TLS HTTP HTTPS SOCKS5"},
    {"module_key": "advanced", "module_label": "Avanzado", "title": "Prompts y versiones", "detail": "Configuración técnica y logs", "search_text": "avanzado prompts versiones logs técnicos"},
]


@router.get("/diagnostics/prompts")
def prompt_execution_diagnostics(
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    executions = db.scalars(
        select(PromptExecution)
        .where(
            PromptExecution.company_id == user.company_id,
            PromptExecution.prompt_purpose.in_(["classification", "extraction", "validation"]),
        )
        .order_by(PromptExecution.id.desc())
        .limit(6)
    ).all()
    execution_ids = [execution.id for execution in executions]
    detail_ids = set(
        db.scalars(
            select(PromptExecutionDetail.prompt_execution_id).where(
                PromptExecutionDetail.company_id == user.company_id,
                PromptExecutionDetail.prompt_execution_id.in_(execution_ids),
            )
        ).all()
    ) if execution_ids else set()

    return JSONResponse(
        {
            "company_id": user.company_id,
            "items": [
                {
                    "id": execution.id,
                    "purpose": execution.prompt_purpose,
                    "status": execution.output_status,
                    "model": execution.model,
                    "validation_errors": execution.validation_errors_json,
                    "duration_ms": execution.duration_ms,
                    "response_excerpt": execution.response_excerpt,
                    "detail_available": execution.id in detail_ids,
                    "detail_url": f"/settings/diagnostics/prompts/{execution.id}",
                    "created_at": execution.created_at.isoformat() if execution.created_at else None,
                }
                for execution in executions
            ],
        }
    )


def _queued_job_response(request: Request, job_id: int, fallback: str = "/settings", result: dict | None = None):
    if "application/json" in (request.headers.get("accept") or ""):
        payload = {"ok": True, "job_id": job_id, "status": "queued", "message": "Trabajo encolado correctamente"}
        if result is not None:
            payload["status"] = "success" if result.get("ok") else "failed"
            payload["result"] = result
            payload["message"] = result.get("message") or payload["message"]
        return JSONResponse(payload)
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _sync_email_sync_state(master_db: Session, user: TenantUser, settings: EmailSettings) -> None:
    now = datetime.now(timezone.utc)
    state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == user.company_id,
            EmailSyncState.channel_key == "email",
        )
    )
    if not state:
        state = EmailSyncState(
            company_id=user.company_id,
            channel_key="email",
            enabled=bool(settings.auto_sync_enabled),
            frequency_seconds=60,
            status="idle",
            next_run_at=now if settings.auto_sync_enabled else None,
        )
        master_db.add(state)
    state.enabled = bool(settings.auto_sync_enabled)
    try:
        state.frequency_seconds = max(int(settings.polling_frequency_minutes or 1), 1) * 60
    except (TypeError, ValueError):
        state.frequency_seconds = 60
    state.mailbox = settings.mailbox or settings.inbox_folder or "INBOX"
    state.source_provider = (settings.provider or "imap").strip().lower() or "imap"
    state.source_host = (settings.imap_host or "").strip() or None
    state.source_username = (settings.imap_username or "").strip() or None
    state.source_connected_email = (settings.connected_email or settings.imap_username or "").strip() or None
    state.next_run_at = now if state.enabled else None
    state.updated_at = now
    master_db.commit()


def _run_email_sync_job_if_needed(request: Request, db: Session, user: TenantUser, job) -> dict | None:  # noqa: ANN001
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.read.inline.start",
        extra={"event": "settings.email.read.inline.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id, "job_id": job.id, "job_type": job.job_type},
    )
    result = execute_job_inline(db, job)
    logger.info(
        "settings.email.read.inline.end",
        extra={
            "event": "settings.email.read.inline.end",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
            "ok": bool(result.get("ok")),
            "found": result.get("found"),
            "saved": result.get("saved"),
            "duplicates": result.get("duplicates"),
            "errors": result.get("errors"),
        },
    )
    return result


def _backfill_job_response_payload(job: BackgroundJob, result: dict, *, from_date: str | None, to_date: str | None) -> dict:
    safe_result = result if isinstance(result, dict) else {}
    continuation_job_id = safe_result.get("continuation_job_id")
    has_more = bool(safe_result.get("has_more"))
    remaining_messages = max(int(safe_result.get("remaining_messages", safe_result.get("remaining") or 0) or 0), 0)
    remaining_limit = max(int(safe_result.get("remaining_limit", safe_result.get("remaining") or 0) or 0), 0)
    total_found = max(int(safe_result.get("total_found", safe_result.get("found") or 0) or 0), 0)
    batch_count = max(int(safe_result.get("batch_count") or 0), 0)
    return {
        "ok": bool(safe_result.get("ok", True)),
        "status": "success" if safe_result.get("ok", True) else "failed",
        "job_id": job.id,
        "continuation_job_id": continuation_job_id,
        "has_more": has_more,
        "remaining": remaining_messages,
        "remaining_messages": remaining_messages,
        "remaining_limit": remaining_limit,
        "batch_count": batch_count,
        "total_found": total_found,
        "found": total_found,
        "saved": int(safe_result.get("saved") or safe_result.get("imported") or 0),
        "duplicates": int(safe_result.get("duplicates") or 0),
        "errors": int(safe_result.get("errors") or 0),
        "from_date": from_date,
        "to_date": to_date,
        "message": safe_result.get("message") or "Backfill IMAP completado",
    }


def _backfill_response(request: Request, payload: dict, fallback: str = "/settings#email-diagnostics"):
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(payload)
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _run_backfill_job_once(request: Request, db: Session, user: TenantUser, job: BackgroundJob, *, from_date: str | None, to_date: str | None) -> dict:
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.backfill.inline.start",
        extra={
            "event": "settings.email.backfill.inline.start",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
        },
    )
    result = execute_job_inline(db, job)
    if not isinstance(result, dict):
        result = {"ok": True, "message": str(result) if result is not None else "Trabajo completado"}
    logger.info(
        "settings.email.backfill.inline.end",
        extra={
            "event": "settings.email.backfill.inline.end",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
            "ok": bool(result.get("ok")),
            "found": result.get("found"),
            "saved": result.get("saved"),
            "duplicates": result.get("duplicates"),
            "errors": result.get("errors"),
            "continuation_job_id": result.get("continuation_job_id"),
        },
    )
    return _backfill_job_response_payload(job, result, from_date=from_date, to_date=to_date)


def _parse_date_input(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def _normalize_receive_form(data: dict) -> dict:
    # The account form is intentionally partial: it only updates connection
    # details. Do not inject checkbox defaults that would overwrite unrelated
    # synchronization preferences when the detected configuration is saved.
    if data.get("email_account_only") == "on":
        return data
    for field in ["auto_sync_enabled", "read_unread_only", "mark_as_read_after_import", "move_after_processing", "imap_use_ssl"]:
        data.setdefault(field, "off")
    data.setdefault("initial_history_mode", "new")
    data.setdefault("initial_history_limit", "50")
    return data


def _clone_email_settings_for_preview(db: Session, company_id: int, data: dict) -> EmailSettings:
    current = get_or_create_settings(db, EmailSettings, company_id)
    preview = EmailSettings(company_id=company_id)
    for column in EmailSettings.__table__.columns:
        if column.name in {"id", "company_id"}:
            continue
        if hasattr(current, column.name):
            setattr(preview, column.name, getattr(current, column.name))
    update_with_form(preview, data, {"imap_password_encrypted", "client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted"})
    try:
        preview.initial_history_limit = max(min(int(preview.initial_history_limit or 50), 100), 1)
    except (TypeError, ValueError):
        preview.initial_history_limit = 50
    preview.initial_history_mode = preview.initial_history_mode if preview.initial_history_mode in {"new", "7d", "30d", "100", "custom"} else "new"
    return preview


def _settings_summary_metrics(db: Session, company_id: int) -> dict:
    """Return only the two counters used by the initial settings summary."""
    row = db.execute(
        select(
            func.coalesce(func.sum(case((Email.status == "error_processing", 1), else_=0)), 0).label("llm_errors"),
            func.coalesce(func.sum(case((Email.detected_type == "dudoso", 1), else_=0)), 0).label("doubtful_emails"),
        ).where(Email.company_id == company_id)
    ).one()._mapping
    return {
        "llm_errors": int(row["llm_errors"] or 0),
        "doubtful_emails": int(row["doubtful_emails"] or 0),
    }


def _prompt_versions_by_template(db: Session, company_id: int) -> tuple[list[PromptTemplate], dict[int, list[PromptVersion]]]:
    prompt_templates = db.scalars(
        select(PromptTemplate).where(PromptTemplate.company_id == company_id).order_by(PromptTemplate.purpose)
    ).all()
    prompt_versions_by_template: dict[int, list[PromptVersion]] = {template.id: [] for template in prompt_templates}
    if prompt_templates:
        prompt_versions_rows = db.scalars(
            select(PromptVersion)
            .where(PromptVersion.template_id.in_([template.id for template in prompt_templates]))
            .order_by(PromptVersion.template_id, PromptVersion.version.desc())
        ).all()
        for prompt_version in prompt_versions_rows:
            prompt_versions_by_template[prompt_version.template_id].append(prompt_version)
    return prompt_templates, prompt_versions_by_template


def _settings_module_context(request: Request, db: Session, user: TenantUser, module_key: str) -> dict:
    """Build context for one settings drawer, only after the drawer is opened."""
    context = {"request": request, "user": user, "settings_fragment": module_key}

    if module_key == "general":
        context["company"] = db.get(Company, user.company_id)
    elif module_key == "identity":
        context.update(
            branding=branding_to_dict(get_or_create_branding(db, user.company_id)),
            can_edit_branding=user.role.name == "Administrador",
        )
    elif module_key == "email":
        company = db.get(Company, user.company_id)
        email_settings = get_or_create_settings(db, EmailSettings, user.company_id)
        llm_settings = get_or_create_settings(db, LLMSettings, user.company_id)
        context.update(
            company=company,
            email=email_settings,
            email_status=email_config_status(email_settings),
            email_templates=email_templates(db, user.company_id),
            email_template_variables=TEMPLATE_VARIABLES,
            can_edit_email=can_edit_email_settings(user),
            can_test_email=can_test_email_settings(user),
            recent_processed_emails=recent_processed_emails_overview(db, user.company_id, days=30, limit=8),
            diagnostics=build_environment_diagnostics(db, user, company=company, email_settings=email_settings, llm_settings=llm_settings),
            is_superadmin=user.role.name == "Superadmin",
        )
    elif module_key == "ai":
        llm_settings = get_or_create_settings(db, LLMSettings, user.company_id)
        scoring_settings = get_or_create_settings(db, ScoringSettings, user.company_id)
        metrics = agent_metrics(db, user.company_id, scoring_settings)
        dashboard = build_settings_dashboard(db, user, metrics, llm_settings, scoring_settings, prompt_templates=[])
        extraction_model_value = resolve_openai_runtime_model(llm_settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)
        classification_model_value = resolve_openai_runtime_model(llm_settings.classification_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)
        validation_model_value = resolve_openai_runtime_model(llm_settings.validation_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)
        model_options = openai_model_option_payload()
        model_values = sorted(OPENAI_MODEL_PRESET_VALUES)
        context.update(
            llm=llm_settings,
            llm_provider_options=[
                {"value": "openai", "label": "OpenAI"},
                {"value": "openai_compatible", "label": "Compatible OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "disabled", "label": "Desactivado"},
            ],
            agent_status=agent_status(llm_settings, metrics),
            agent_metrics=metrics,
            agent_improvements=improvement_suggestions(db, user.company_id),
            model_options=model_options,
            model_values=model_values,
            extraction_model_options=model_options,
            extraction_model_values=model_values,
            classification_model_value=classification_model_value,
            extraction_model_value=extraction_model_value,
            validation_model_value=validation_model_value,
            extraction_model_label=openai_model_label(extraction_model_value),
            extraction_model_description=openai_model_description(extraction_model_value),
            reasoning_effort_options=reasoning_effort_option_payload(),
            dashboard={"ai_module": dashboard["ai_module"]},
            is_superadmin=user.role.name == "Superadmin",
            mask_secret=mask_secret,
        )
    elif module_key == "scoring":
        llm_settings = get_or_create_settings(db, LLMSettings, user.company_id)
        scoring_settings = get_or_create_settings(db, ScoringSettings, user.company_id)
        dashboard = build_settings_dashboard(db, user, {"llm_errors": 0, "doubtful_emails": 0}, llm_settings, scoring_settings, prompt_templates=[])
        context.update(llm=llm_settings, scoring=scoring_settings, dashboard={"scoring_module": dashboard["scoring_module"]})
    elif module_key == "decision":
        context["decision"] = get_or_create_settings(db, DecisionSettings, user.company_id)
    elif module_key == "export":
        context["export"] = get_or_create_settings(db, ExportSettings, user.company_id)
    elif module_key == "ftp":
        context.update(ftp=get_or_create_settings(db, FTPSettings, user.company_id), mask_secret=mask_secret)
    elif module_key == "proxies":
        context.update(
            proxy_connections=db.scalars(
                select(ProxyConnection)
                .where(ProxyConnection.company_id == user.company_id)
                .order_by(ProxyConnection.name.asc(), ProxyConnection.id.asc())
            ).all(),
            can_edit_proxies=can_edit_proxies(user),
            mask_secret=mask_secret,
        )
    elif module_key == "advanced":
        prompts, prompt_versions = _prompt_versions_by_template(db, user.company_id)
        context.update(
            prompts=prompts,
            prompt_versions=prompt_versions,
            dashboard={"prompt_templates": prompts},
        )
    return context


@router.get("/module/{module_key}")
def settings_module(module_key: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if module_key not in SETTINGS_MODULE_KEYS:
        raise HTTPException(status_code=404, detail="Módulo de configuración no encontrado")
    return templates.TemplateResponse(request, "settings/index.html", _settings_module_context(request, db, user, module_key))


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm_settings = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring_settings = get_or_create_settings(db, ScoringSettings, user.company_id)
    decision_settings = get_or_create_settings(db, DecisionSettings, user.company_id)
    company = db.get(Company, user.company_id)
    email_settings = get_or_create_settings(db, EmailSettings, user.company_id)
    ftp_settings = get_or_create_settings(db, FTPSettings, user.company_id)
    proxy_connection_count = db.scalar(
        select(func.count(ProxyConnection.id)).where(ProxyConnection.company_id == user.company_id)
    ) or 0
    export_settings = get_or_create_settings(db, ExportSettings, user.company_id)
    branding_settings = get_or_create_branding(db, user.company_id)
    dashboard = build_settings_dashboard(
        db,
        user,
        _settings_summary_metrics(db, user.company_id),
        llm_settings,
        scoring_settings,
        company=company,
        branding=branding_settings,
        email=email_settings,
        ftp=ftp_settings,
        proxy_connection_count=proxy_connection_count,
        export=export_settings,
        decision=decision_settings,
        prompt_templates=[],
        prompt_count=db.scalar(select(func.count(PromptTemplate.id)).where(PromptTemplate.company_id == user.company_id)) or 0,
    )
    return templates.TemplateResponse(
        request,
        "settings/index.html",
        {
            "request": request,
            "user": user,
            "company": company,
            "settings_fragment": "",
            "settings_search_catalog": SETTINGS_SEARCH_CATALOG,
            "dashboard": dashboard,
        },
    )


@router.get("/diagnostics/prompts/{execution_id}")
def prompt_execution_detail(
    execution_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    execution = db.scalar(
        select(PromptExecution).where(
            PromptExecution.id == execution_id,
            PromptExecution.company_id == user.company_id,
        )
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Ejecución IA no encontrada")

    detail = db.scalar(
        select(PromptExecutionDetail).where(
            PromptExecutionDetail.prompt_execution_id == execution.id,
            PromptExecutionDetail.company_id == user.company_id,
        )
    )

    def decode_json(raw: str | None):
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value

    return JSONResponse(
        {
            "company_id": user.company_id,
            "execution": {
                "id": execution.id,
                "purpose": execution.prompt_purpose,
                "prompt_name": execution.prompt_name,
                "prompt_version": execution.prompt_version,
                "model": execution.model,
                "status": execution.output_status,
                "validation_errors": decode_json(execution.validation_errors_json),
                "input_reference": execution.input_reference,
                "input_tokens": execution.input_tokens,
                "output_tokens": execution.output_tokens,
                "duration_ms": execution.duration_ms,
                "response_hash": execution.response_hash,
                "response_excerpt": execution.response_excerpt,
                "started_at": execution.started_at.isoformat() if execution.started_at else None,
                "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
            },
            "detail_available": detail is not None,
            "payload_stored": bool(detail and detail.user_input_text is not None),
            "detail": {
                "system_prompt": detail.system_prompt_text if detail else None,
                "user_input": detail.user_input_text if detail else None,
                "assistant_output": detail.assistant_output_text if detail else None,
                "reasoning_summary": detail.reasoning_summary if detail else None,
                "decision_summary": detail.decision_summary if detail else None,
                "effective_parameters": decode_json(detail.effective_parameters_json if detail else None),
                "provider_metadata": decode_json(detail.provider_metadata_json if detail else None),
                "is_anonymized": bool(detail and detail.is_anonymized),
                "created_at": detail.created_at.isoformat() if detail and detail.created_at else None,
            },
            "notice": "Las credenciales se redactan siempre. El razonamiento privado interno del modelo no se almacena; solo se muestra un resumen explícito si el proveedor lo devuelve.",
        }
    )
    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request,
            "user": user,
            "company": company,
            "settings_fragment": "",
            "settings_search_catalog": SETTINGS_SEARCH_CATALOG,
            "dashboard": dashboard,
        },
    )


def build_settings_dashboard(
    db: Session,
    user: TenantUser,
    metrics: dict,
    llm: LLMSettings,
    scoring: ScoringSettings,
    *,
    company: Company | None = None,
    branding: BrandingSettings | None = None,
    email: EmailSettings | None = None,
    ftp: FTPSettings | None = None,
    proxy_connection_count: int | None = None,
    export: ExportSettings | None = None,
    decision: DecisionSettings | None = None,
    prompt_templates: list[PromptTemplate] | None = None,
    prompt_count: int | None = None,
) -> dict:
    company = company if company is not None else db.get(Company, user.company_id)
    branding = branding if branding is not None else get_or_create_branding(db, user.company_id)
    email = email if email is not None else get_or_create_settings(db, EmailSettings, user.company_id)
    email_status = email_config_status(email)
    ftp = ftp if ftp is not None else get_or_create_settings(db, FTPSettings, user.company_id)
    if proxy_connection_count is None:
        proxy_connection_count = db.scalar(
            select(func.count(ProxyConnection.id)).where(ProxyConnection.company_id == user.company_id)
        ) or 0
    export = export if export is not None else get_or_create_settings(db, ExportSettings, user.company_id)
    decision = decision if decision is not None else get_or_create_settings(db, DecisionSettings, user.company_id)
    dashboard_counts = db.execute(
        select(
            select(func.count(Customer.id)).where(Customer.company_id == user.company_id).scalar_subquery().label("customer_count"),
            select(func.count(Product.id)).where(Product.company_id == user.company_id).scalar_subquery().label("product_count"),
            select(func.count(InputChannel.id)).where(InputChannel.company_id == user.company_id, InputChannel.is_active == True).scalar_subquery().label("active_channels_count"),  # noqa: E712
        )
    ).one()._mapping
    customer_count = int(dashboard_counts["customer_count"] or 0)
    product_count = int(dashboard_counts["product_count"] or 0)
    active_channels_count = int(dashboard_counts["active_channels_count"] or 0)
    if prompt_count is None:
        prompt_count = len(prompt_templates) if prompt_templates is not None else db.scalar(
            select(func.count(PromptTemplate.id)).where(PromptTemplate.company_id == user.company_id)
        ) or 0
    prompt_templates = prompt_templates if prompt_templates is not None else []

    def state(key: str, label: str, kind: str, summary: str, action: str) -> dict:
        return {"key": key, "label": label, "state": kind, "summary": summary, "action": action}

    modules = [
        state(
            "general",
            "General",
            "ready" if company and company.name else "pending",
            f"{company.name if company else 'Sin empresa'} · {(company.currency or 'EUR') if company else 'EUR'} · {('activa' if company and company.active else 'inactiva')}",
            "Configurar",
        ),
        state("identity", "Identidad", "ready" if branding.app_name and (branding.logo_url or branding.dark_logo_url) else "warning" if branding.app_name else "pending", f"{branding.app_name} · {branding.secondary_claim or 'sin claim secundario'}", "Configurar"),
        state("channels", "Canales", "ready" if active_channels_count else "pending", f"{active_channels_count} canal activo" if active_channels_count == 1 else f"{active_channels_count} canales activos", "Abrir"),
        state("ai", "Agente IA", "ready" if llm.provider != "disabled" and llm.api_key_encrypted and llm.last_test_ok is not False else "warning" if llm.api_key_encrypted else "pending", f"{llm.provider or 'sin proveedor'} · {resolve_openai_runtime_model(llm.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)} · {llm.last_test_message or 'sin prueba reciente'}", "Configurar"),
        state("customers-products", "Clientes y productos", "ready" if customer_count and product_count else "warning" if customer_count or product_count else "pending", f"{customer_count} clientes · {product_count} productos", "Abrir"),
        state("scoring", "Confianza y automatización", "ready", f"Alta confianza desde {scoring.safe_threshold}% · auto-confirmar {'sí' if llm.allow_auto_confirm else 'no'}", "Configurar"),
        state("decision", "Motor de decisión", "ready" if decision.enable_exact_match else "warning", f"Prioridad {decision.exact_priority} a {decision.llm_priority} · modo {decision.learning_mode}", "Configurar"),
        state("export", "Exportación", "ready" if export.file_type and export.filename_template else "pending", f"{export.file_type.upper() if export.file_type else 'Sin formato'} · {export.filename_template or 'sin plantilla'}", "Configurar"),
        state("ftp", "FTP/SFTP", "ready" if ftp.host and ftp.username else "pending", f"{ftp.connection_type.upper()} · {ftp.host or 'host pendiente'}", "Configurar"),
        state("proxies", "Proxies", "ready", f"{proxy_connection_count} perfiles · tráfico del gateway inactivo", "Configurar"),
        state("alerts", "Alertas", "ready", f"{metrics['llm_errors']} errores · {metrics['doubtful_emails']} dudosos", "Ver"),
        state("users", "Usuarios y permisos", "ready", "Roles y accesos activos", "Abrir"),
        state("advanced", "Avanzado", "optional" if user.role.name == "Superadmin" else "locked", f"{prompt_count} prompts · logs técnicos", "Abrir"),
    ]
    visible_modules = [module for module in modules if module["state"] != "locked"]
    configured = len([module for module in visible_modules if module["state"] in {"ready", "warning"}])
    pending = len([module for module in visible_modules if module["state"] == "pending"])
    errors = len([module for module in visible_modules if module["state"] == "error"])
    progress = round((configured * 100) / len(visible_modules)) if visible_modules else 0
    module_map = {module["key"]: module for module in modules}
    checklist = [
        {"key": "general", "label": "Empresa e identidad básica", "state": "done" if module_map.get("general", {}).get("state") == "ready" and module_map.get("identity", {}).get("state") in ("ready", "warning") else "pending", "open_settings": "general"},
        {"key": "channels", "label": "Canal de entrada conectado", "state": "done" if module_map.get("channels", {}).get("state") == "ready" else "pending", "url": "/settings/channels"},
        {"key": "ai", "label": "Agente IA configurado", "state": "done" if module_map.get("ai", {}).get("state") in ("ready", "warning") else "pending", "open_settings": "ai"},
        {"key": "customers-products", "label": "Clientes y productos cargados", "state": "done" if module_map.get("customers-products", {}).get("state") in ("ready", "warning") else "pending", "url": "/products"},
        {"key": "scoring", "label": "Scoring definido", "state": "done" if module_map.get("scoring", {}).get("state") == "ready" else "pending", "open_settings": "scoring"},
        {"key": "decision", "label": "Motor de decisión activo", "state": "done" if module_map.get("decision", {}).get("state") in ("ready", "warning") else "pending", "open_settings": "decision"},
        {"key": "export", "label": "Exportación configurada", "state": "done" if module_map.get("export", {}).get("state") == "ready" else "pending", "open_settings": "export"},
        {"key": "ftp", "label": "FTP/SFTP configurado", "state": "done" if module_map.get("ftp", {}).get("state") == "ready" else "pending", "open_settings": "ftp"},
    ]
    return {
        "progress": progress,
        "configured": configured,
        "pending": pending,
        "errors": errors,
        "modules": modules,
        "checklist": checklist,
        "customer_count": customer_count,
        "product_count": product_count,
        "active_channels_count": active_channels_count,
        "prompt_templates": prompt_templates,
        "prompt_templates_count": prompt_count,
        "email_status": email_status,
        "email": email,
        "llm": llm,
        "ftp": ftp,
        "proxy_connection_count": proxy_connection_count,
        "export": export,
        "decision": decision,
        "branding": branding,
        "company": company,
        "ai_module": module_map.get("ai"),
        "scoring_module": module_map.get("scoring"),
    }


def build_environment_diagnostics(
    db: Session,
    user: TenantUser,
    *,
    company: Company | None = None,
    email_settings: EmailSettings | None = None,
    llm_settings: LLMSettings | None = None,
) -> dict:
    company = company if company is not None else db.get(Company, user.company_id)
    email_settings = email_settings if email_settings is not None else get_or_create_settings(db, EmailSettings, user.company_id)
    llm_settings = llm_settings if llm_settings is not None else get_or_create_settings(db, LLMSettings, user.company_id)
    company_id = user.company_id

    def count_for(model, *conditions):  # noqa: ANN001
        return select(func.count(model.id)).where(model.company_id == company_id, *conditions).scalar_subquery()

    diagnostics_stats = db.execute(
        select(
            select(func.max(AuditLog.created_at)).where(AuditLog.company_id == company_id, AuditLog.action == "demo.seed").scalar_subquery().label("last_seed_at"),
            count_for(Customer).label("customers_total"),
            count_for(Product).label("products_total"),
            count_for(Order).label("orders_total"),
            count_for(Email).label("emails_total"),
            count_for(Email, Email.agent_status != "not_processed").label("processed_emails_total"),
            count_for(InboundMessage).label("inbound_total"),
            count_for(InputChannel, InputChannel.is_active == True).label("active_channels_total"),  # noqa: E712
            count_for(BackgroundJob).label("jobs_total"),
            count_for(BackgroundJob, BackgroundJob.status == "queued").label("jobs_queued"),
            count_for(BackgroundJob, BackgroundJob.status == "running").label("jobs_running"),
            count_for(BackgroundJob, BackgroundJob.status == "retrying").label("jobs_retrying"),
            count_for(BackgroundJob, BackgroundJob.status == "failed").label("jobs_failed"),
            count_for(BackgroundJob, BackgroundJob.status == "cancelled").label("jobs_cancelled"),
            count_for(BackgroundJob, BackgroundJob.status == "success").label("jobs_success"),
        )
    ).one()._mapping
    last_seed_at = diagnostics_stats["last_seed_at"]
    customers_total = int(diagnostics_stats["customers_total"] or 0)
    products_total = int(diagnostics_stats["products_total"] or 0)
    orders_total = int(diagnostics_stats["orders_total"] or 0)
    emails_total = int(diagnostics_stats["emails_total"] or 0)
    processed_emails_total = int(diagnostics_stats["processed_emails_total"] or 0)
    inbound_total = int(diagnostics_stats["inbound_total"] or 0)
    active_channels_total = int(diagnostics_stats["active_channels_total"] or 0)
    jobs_total = int(diagnostics_stats["jobs_total"] or 0)
    jobs_queued = int(diagnostics_stats["jobs_queued"] or 0)
    jobs_running = int(diagnostics_stats["jobs_running"] or 0)
    jobs_retrying = int(diagnostics_stats["jobs_retrying"] or 0)
    jobs_failed = int(diagnostics_stats["jobs_failed"] or 0)
    jobs_cancelled = int(diagnostics_stats["jobs_cancelled"] or 0)
    jobs_success = int(diagnostics_stats["jobs_success"] or 0)
    email_status = email_config_status(email_settings)
    llm_ready = bool(llm_settings.provider and llm_settings.provider != "disabled" and llm_settings.api_key_encrypted and llm_settings.last_test_ok is not False)
    migration_report = tenant_migration_report(db, user.company_id)
    database_url = get_settings().database_url
    split = urlsplit(database_url)
    if split.password:
        netloc = split.netloc.replace(split.password, "***")
        database_url = urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))
    return {
        "environment": get_settings().environment,
        "database_url": database_url,
        "database_label": split.path.rsplit("/", 1)[-1] if split.path else database_url,
        "company_id": user.company_id,
        "company_name": company.name if company else "",
        "company_active": bool(company.active) if company else False,
        "user_id": user.id,
        "email": user.email,
        "role": user.role.name,
        "app_name": get_settings().app_name,
        "last_seed_at": last_seed_at,
        "imap_ready": email_status["imap_ready"],
        "smtp_ready": email_status["smtp_ready"],
        "last_imap_test_ok": email_settings.last_imap_test_ok,
        "last_imap_test_at": email_settings.last_imap_test_at,
        "last_sync_at": email_settings.last_sync_at,
        "last_sync_ok": email_settings.last_sync_ok,
        "llm_provider": llm_settings.provider,
        "llm_ready": llm_ready,
        "tenant_schema_version": migration_report["version"],
        "tenant_schema_name": migration_report["name"],
        "tenant_schema_checksum": migration_report["checksum"],
        "tenant_schema_execution_ms": migration_report["execution_ms"],
        "tenant_schema_application_version": migration_report["application_version"],
        "tenant_schema_current_version": migration_report["current_version"],
        "tenant_schema_current_name": migration_report["current_name"],
        "tenant_schema_current_checksum": migration_report["current_checksum"],
        "tenant_schema_status": migration_report["status"],
        "tenant_schema_last_checked_at": migration_report["last_checked_at"],
        "tenant_schema_applied_at": migration_report["applied_at"],
        "tenant_schema_last_error": migration_report["last_error"],
        "tenant_schema_is_current": migration_report["is_current"],
        "customers_total": customers_total,
        "products_total": products_total,
        "orders_total": orders_total,
        "emails_total": emails_total,
        "processed_emails_total": processed_emails_total,
        "inbound_total": inbound_total,
        "active_channels_total": active_channels_total,
        "jobs_total": jobs_total,
        "jobs_queued": jobs_queued,
        "jobs_running": jobs_running,
        "jobs_retrying": jobs_retrying,
        "jobs_failed": jobs_failed,
        "jobs_cancelled": jobs_cancelled,
        "jobs_success": jobs_success,
    }


def can_edit_email_settings(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


def can_test_email_settings(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Supervisor", "Superadmin"}


def can_edit_proxies(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


_PROXY_PROTOCOLS = {"http", "https", "socks5", "other"}
_PROXY_TLS_MODES = {"verify", "required", "disabled"}
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validate_proxy_host(raw_host: str) -> str:
    host = (raw_host or "").strip()
    if not host or "/" in host or "\\" in host or any(character.isspace() for character in host):
        raise ValueError("Introduce un host o una IP válida, sin protocolo ni ruta.")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        import ipaddress

        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME_RE.fullmatch(host):
            raise ValueError("Introduce un host o una IP válida, sin protocolo ni ruta.")
    return host


def _proxy_form_values(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("El nombre del perfil es obligatorio y no puede superar 120 caracteres.")
    host = _validate_proxy_host(str(data.get("proxy_host") or ""))
    try:
        port = int(data.get("proxy_port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("El puerto del proxy debe ser un número entre 1 y 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("El puerto del proxy debe ser un número entre 1 y 65535.")
    protocol = str(data.get("proxy_protocol") or "").strip().lower()
    if protocol not in _PROXY_PROTOCOLS:
        raise ValueError("El tipo de proxy no es válido.")
    tls_mode = str(data.get("tls_mode") or "verify").strip().lower()
    if tls_mode not in _PROXY_TLS_MODES:
        raise ValueError("El modo TLS no es válido.")
    return {
        "name": name,
        "proxy_host": host,
        "proxy_port": port,
        "proxy_protocol": protocol,
        "username": str(data.get("username") or "").strip() or None,
        "tls_mode": tls_mode,
        "enabled": str(data.get("enabled") or "").lower() in {"on", "true", "1"},
        "notes": str(data.get("notes") or "").strip() or None,
    }


def _proxy_health_result(connection: ProxyConnection) -> tuple[bool, str, str]:
    """Check only the remote gateway health endpoint, never a DB destination."""
    if connection.proxy_protocol not in {"http", "https"}:
        return False, "unsupported_protocol", "La prueba de salud solo admite proxies HTTP o HTTPS."

    host = _validate_proxy_host(connection.proxy_host)
    host_for_url = f"[{host}]" if ":" in host else host
    url = f"{connection.proxy_protocol}://{host_for_url}:{connection.proxy_port}/health"
    password = decrypt_secret(connection.password_encrypted)
    auth = (connection.username, password) if connection.username and password else None
    verify_tls = connection.tls_mode != "disabled"

    try:
        timeout = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=2.0)
        with httpx.Client(timeout=timeout, verify=verify_tls, follow_redirects=False, trust_env=False) as client:
            response = client.get(
                url,
                auth=auth,
                headers={"Accept": "application/json", "User-Agent": "Anchi-Proxy-Health/1.0"},
            )
    except httpx.TimeoutException:
        return False, "timeout", "El gateway no respondió dentro del tiempo esperado."
    except httpx.HTTPError:
        return False, "unreachable", "No se pudo conectar con el gateway configurado."
    except ValueError as exc:
        return False, "invalid_configuration", str(exc)

    if response.status_code == 401:
        return False, "unauthorized", "El gateway rechazó las credenciales del perfil."
    if response.status_code != 200:
        return False, "unavailable", "El gateway respondió, pero su endpoint de salud no está disponible."
    try:
        payload = response.json()
    except ValueError:
        return False, "invalid_response", "El gateway devolvió una respuesta no válida."
    if payload.get("service") != "anchi-proxy" or payload.get("traffic_enabled") is not False:
        return False, "invalid_response", "La respuesta no corresponde al gateway de Anchi bloqueado para datos."
    return True, "ready", "Gateway accesible. El tráfico de datos sigue desactivado."


@router.api_route("/proxies", methods=["PUT", "POST"])
async def save_proxy_connection(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_proxies(user):
        return JSONResponse({"ok": False, "message": "Solo Administrador puede configurar proxies."}, status_code=403)
    data = await request_data(request)
    try:
        values = _proxy_form_values(data)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)

    raw_id = str(data.get("id") or "").strip()
    connection = None
    if raw_id:
        try:
            connection = db.scalar(
                select(ProxyConnection).where(
                    ProxyConnection.id == int(raw_id),
                    ProxyConnection.company_id == user.company_id,
                )
            )
        except ValueError:
            connection = None
        if connection is None:
            return JSONResponse({"ok": False, "message": "No se encontró el perfil de proxy."}, status_code=404)
    else:
        connection = ProxyConnection(company_id=user.company_id)
        db.add(connection)

    for key, value in values.items():
        setattr(connection, key, value)
    password = str(data.get("password") or "").strip()
    if password and password not in {"********", "••••••••"}:
        connection.password_encrypted = encrypt_secret(password)
    connection.updated_by = resolve_updated_by_id(db, user)
    connection.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
            return JSONResponse({"ok": False, "message": "Ya existe un perfil con ese nombre."}, status_code=409)
        raise
    log_action(db, company_id=user.company_id, user=user, action="settings.proxy.save", entity_type="proxy_connection", entity_id=connection.id, message="Perfil de proxy guardado")
    return redirect_or_json(request, {"ok": True, "id": connection.id, "message": "Perfil de proxy guardado."}, "proxies")


@router.post("/proxies/{proxy_id}/test")
def test_proxy_connection(proxy_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_proxies(user):
        return JSONResponse({"ok": False, "message": "No tienes permisos para probar este perfil."}, status_code=403)
    connection = db.scalar(
        select(ProxyConnection).where(ProxyConnection.id == proxy_id, ProxyConnection.company_id == user.company_id)
    )
    if connection is None:
        return JSONResponse({"ok": False, "message": "No se encontró el perfil de proxy."}, status_code=404)
    tested_at = datetime.now(timezone.utc)
    try:
        ok, status, message = _proxy_health_result(connection)
    except ValueError as exc:
        ok, status, message = False, "invalid_configuration", str(exc)
    connection.last_test_at = tested_at
    connection.last_test_ok = ok
    connection.last_test_message = message
    db.commit()
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(
            {"ok": ok, "status": status, "message": message, "checked_at": tested_at.isoformat()},
            status_code=200 if ok else 502,
        )
    return RedirectResponse("/settings#proxies", status_code=303)


async def request_data(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    return dict(await request.form())


def save_email_section(db: Session, settings: EmailSettings, data: dict, user: TenantUser, fields: list[str], secret_fields: set[str] | None = None) -> None:
    update_with_form(settings, {field: data.get(field, "") for field in fields if field in data or field in (secret_fields or set())}, secret_fields)
    settings.updated_by = resolve_updated_by_id(db, user)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()


def redirect_or_json(request: Request, payload: dict, anchor: str = "email"):
    if request.method == "PUT" or "application/json" in request.headers.get("content-type", ""):
        return JSONResponse(payload)
    referer = request.headers.get("referer", "")
    if "channels" in referer:
        return RedirectResponse("/settings/channels?focus=email", status_code=303)
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


@router.get("/email")
def get_email_settings(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(serialize_email_settings(db, user.company_id))


@router.post("/email/autoconfig")
def autoconfigure_email(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    user: TenantUser = Depends(current_user),
):
    """Discover and verify a mailbox without persisting its password."""

    if user.role.name not in {"Administrador", "Superadmin"}:
        return JSONResponse({"ok": False, "message": "Solo Administrador puede configurar el correo."}, status_code=403)
    try:
        result = detect_email_configuration(email, password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001
        request_id = getattr(request.state, "request_id", None) if request is not None else None
        logger.exception(
            "settings.email.autoconfig.failed",
            extra={
                "event": "settings.email.autoconfig.failed",
                "request_id": request_id,
                "company_id": user.company_id,
                "user_id": user.id,
            },
        )
        return JSONResponse(
            {"ok": False, "message": "No se ha podido comprobar la configuración de correo en este momento."},
            status_code=502,
        )
    logger.info(
        "settings.email.autoconfig.completed",
        extra={
            "event": "settings.email.autoconfig.completed",
            "request_id": getattr(request.state, "request_id", None) if request is not None else None,
            "company_id": user.company_id,
            "user_id": user.id,
            "domain": result.get("domain"),
            "provider": result.get("provider"),
            "detected": result.get("detected"),
            "can_use_in_anchi": result.get("can_use_in_anchi"),
        },
    )
    return JSONResponse(result)


@router.api_route("/email/receive", methods=["PUT", "POST"])
async def update_email_receive(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.receive.start",
        extra={
            "event": "settings.email.receive.start",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
        },
    )
    data = _normalize_receive_form(await request_data(request))
    logger.info(
        "settings.email.receive.form_parsed",
        extra={
            "event": "settings.email.receive.form_parsed",
            "request_id": request_id,
            "company_id": user.company_id,
            "provider": (data.get("provider") or "").strip().lower(),
            "host": (data.get("imap_host") or "").strip(),
            "port": data.get("imap_port"),
            "security": (data.get("imap_security") or "").strip().lower(),
            "has_password": bool((data.get("imap_password_encrypted") or "").strip()),
            "history_mode": (data.get("initial_history_mode") or "").strip().lower(),
            "history_limit": data.get("initial_history_limit"),
        },
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    logger.info(
        "settings.email.receive.settings_loaded",
        extra={
            "event": "settings.email.receive.settings_loaded",
            "request_id": request_id,
            "company_id": user.company_id,
            "settings_id": settings.id,
        },
    )
    fields = ["provider", "imap_host", "imap_port", "imap_security", "imap_use_ssl", "imap_username", "imap_password_encrypted", "inbox_folder", "processed_folder", "error_folder", "no_order_folder", "doubtful_folder", "read_limit", "test_read_limit", "auto_sync_enabled", "read_unread_only", "read_from_date", "initial_history_mode", "initial_history_limit", "mark_as_read_after_import", "move_after_processing", "post_process_action", "polling_frequency_minutes", "client_id", "client_secret_encrypted", "tenant_id", "redirect_uri", "oauth_scopes", "mailbox", "access_token_encrypted", "refresh_token_encrypted", "connected_email"]
    save_email_section(db, settings, data, user, fields, {"imap_password_encrypted", "client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted"})
    logger.info(
        "settings.email.receive.saved",
        extra={
            "event": "settings.email.receive.saved",
            "request_id": request_id,
            "company_id": user.company_id,
            "settings_id": settings.id,
            "provider": (settings.provider or "").strip().lower(),
            "host": (settings.imap_host or "").strip(),
            "port": settings.imap_port,
            "security": (settings.imap_security or "").strip().lower(),
            "has_password": bool(settings.imap_password_encrypted),
        },
    )
    allowed_frequencies = {5, 10, 15, 30, 60}
    try:
        frequency = int(settings.polling_frequency_minutes or 1)
    except (TypeError, ValueError):
        frequency = 5
    settings.polling_frequency_minutes = frequency if frequency in allowed_frequencies else 5
    try:
        settings.initial_history_limit = max(min(int(settings.initial_history_limit or 50), 100), 1)
    except (TypeError, ValueError):
        settings.initial_history_limit = 50
    settings.initial_history_mode = settings.initial_history_mode if settings.initial_history_mode in {"new", "7d", "30d", "100", "custom"} else "new"
    try:
        _sync_email_sync_state(master_db, user, settings)
    except Exception:
        master_db.rollback()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.receive.update", entity_type="settings", entity_id=settings.id, message="Configuracion de recepcion actualizada")
    return redirect_or_json(request, {"ok": True, "receive": serialize_email_settings(db, user.company_id)["receive"]}, "email-receive")


@router.post("/email/disconnect")
def disconnect_email_account(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-account", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in [
        "provider",
        "imap_host",
        "imap_username",
        "imap_password_encrypted",
        "client_id",
        "client_secret_encrypted",
        "tenant_id",
        "redirect_uri",
        "oauth_scopes",
        "mailbox",
        "access_token_encrypted",
        "refresh_token_encrypted",
        "connected_email",
        "processed_folder",
        "error_folder",
        "no_order_folder",
        "doubtful_folder",
        "read_from_date",
    ]:
        setattr(settings, field, None)
    settings.imap_port = 993
    settings.imap_security = "ssl_tls"
    settings.imap_use_ssl = True
    settings.inbox_folder = "INBOX"
    settings.auto_sync_enabled = False
    settings.read_unread_only = True
    settings.mark_as_read_after_import = False
    settings.move_after_processing = False
    settings.updated_by = resolve_updated_by_id(db, user)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    try:
        _sync_email_sync_state(master_db, user, settings)
    except Exception:
        master_db.rollback()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.disconnect", entity_type="settings", entity_id=settings.id, message="Cuenta de correo desconectada")
    return RedirectResponse("/settings#email-account", status_code=303)


@router.api_route("/email/send", methods=["PUT", "POST"])
async def update_email_send(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in ["smtp_enabled", "save_internal_copy", "preserve_thread_headers"]:
        data.setdefault(field, "off")
    fields = ["smtp_enabled", "smtp_provider", "smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_password_encrypted", "from_email", "from_name", "reply_to", "default_cc", "default_bcc", "save_internal_copy", "preserve_thread_headers"]
    save_email_section(db, settings, data, user, fields, {"smtp_password_encrypted"})
    log_action(db, company_id=user.company_id, user=user, action="settings.email.send.update", entity_type="settings", entity_id=settings.id, message="Configuracion de envio actualizada")
    return redirect_or_json(request, {"ok": True, "send": serialize_email_settings(db, user.company_id)["send"]}, "email-send")


@router.api_route("/email/processing", methods=["PUT", "POST"])
async def update_email_processing(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    bool_fields = ["auto_process_on_fetch", "process_only_with_attachments", "process_only_with_pdf", "process_without_attachments", "process_read_emails", "avoid_duplicates_by_message_id", "allow_reprocess", "auto_create_order_if_detected", "always_human_review", "mark_doubtful_below_threshold", "mark_no_order_if_detected"]
    for field in bool_fields:
        data.setdefault(field, "off")
    fields = bool_fields + ["action_order_detected", "action_no_order", "action_doubtful", "action_error", "minimum_score_auto_order", "visible_states"]
    save_email_section(db, settings, data, user, fields)
    log_action(db, company_id=user.company_id, user=user, action="settings.email.processing.update", entity_type="settings", entity_id=settings.id, message="Reglas de procesamiento de correo actualizadas")
    return redirect_or_json(request, {"ok": True, "processing": serialize_email_settings(db, user.company_id)["processing"]}, "email-processing")


@router.api_route("/email/ui", methods=["PUT", "POST"])
async def update_email_ui(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    bool_fields = ["show_summary_cards", "show_score_column", "show_customer_column", "show_attachments_column", "show_order_column", "show_reply_button", "show_process_button"]
    for field in bool_fields:
        data.setdefault(field, "off")
    fields = ["default_filter", "default_date_range", "default_page_size", "default_sort"] + bool_fields
    save_email_section(db, settings, data, user, fields)
    log_action(db, company_id=user.company_id, user=user, action="settings.email.ui.update", entity_type="settings", entity_id=settings.id, message="Preferencias de bandeja de correo actualizadas")
    return redirect_or_json(request, {"ok": True, "ui": serialize_email_settings(db, user.company_id)["ui"]}, "email-ui")


@router.get("/email/templates")
def list_email_templates(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(serialize_email_settings(db, user.company_id)["templates"])


@router.post("/email/templates")
def create_email_template(
    key: str = Form(...),
    name: str = Form(...),
    template_type: str = Form("other"),
    subject_template: str = Form("Re: {asunto_original}"),
    body_template: str = Form(...),
    active: str = Form("off"),
    is_default_for_type: str = Form("off"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-templates", status_code=303)
    template = EmailTemplate(company_id=user.company_id, key=key.strip(), name=name.strip(), template_type=template_type, subject_template=subject_template, body_template=body_template, active=active == "on", is_default_for_type=is_default_for_type == "on", updated_by=resolve_updated_by_id(db, user))
    db.add(template)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.template.create", entity_type="email_template", entity_id=template.id, message=f"Plantilla creada: {template.key}")
    return RedirectResponse("/settings#email-templates", status_code=303)


@router.api_route("/email/templates/{template_id}", methods=["PUT", "POST"])
async def update_email_template(template_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar plantillas."}, status_code=403)
    template = db.get(EmailTemplate, template_id)
    if not template or template.company_id != user.company_id:
        return JSONResponse({"error": "Plantilla no encontrada."}, status_code=404)
    data = await request_data(request)
    data.setdefault("active", "off")
    data.setdefault("is_default_for_type", "off")
    update_with_form(template, data)
    template.updated_by = resolve_updated_by_id(db, user)
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.template.update", entity_type="email_template", entity_id=template.id, message=f"Plantilla actualizada: {template.key}")
    return redirect_or_json(request, {"ok": True}, "email-templates")


@router.api_route("/email/templates/{template_id}", methods=["DELETE"])
def delete_email_template(template_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede eliminar plantillas."}, status_code=403)
    template = db.get(EmailTemplate, template_id)
    if not template or template.company_id != user.company_id:
        return JSONResponse({"error": "Plantilla no encontrada."}, status_code=404)
    template.active = False
    template.updated_by = resolve_updated_by_id(db, user)
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.template.disable", entity_type="email_template", entity_id=template.id, message=f"Plantilla desactivada: {template.key}")
    return JSONResponse({"ok": True})


@router.post("/email/templates/{template_id}/duplicate")
def duplicate_email_template(template_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if can_edit_email_settings(user):
        template = db.get(EmailTemplate, template_id)
        if template and template.company_id == user.company_id:
            copy = EmailTemplate(company_id=user.company_id, key=f"{template.key}_copia", name=f"{template.name} copia", template_type=template.template_type, subject_template=template.subject_template, body_template=template.body_template, active=False, is_default_for_type=False, updated_by=resolve_updated_by_id(db, user))
            db.add(copy)
            db.commit()
            log_action(db, company_id=user.company_id, user=user, action="settings.email.template.duplicate", entity_type="email_template", entity_id=copy.id, message=f"Plantilla duplicada: {template.key}")
    return RedirectResponse("/settings#email-templates", status_code=303)


@router.post("/email/templates/reset-defaults")
def reset_email_templates(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if can_edit_email_settings(user):
        ensure_default_email_templates(db, user.company_id, user.id)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="settings.email.templates.reset", entity_type="email_template", message="Plantillas de correo restauradas")
    return RedirectResponse("/settings#email-templates", status_code=303)


@router.get("/email/signature")
def get_email_signature(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(serialize_email_settings(db, user.company_id)["signature"])


@router.api_route("/email/signature", methods=["PUT", "POST"])
async def update_email_signature(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la firma."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in ["use_signature", "include_logo_in_signature"]:
        data.setdefault(field, "off")
    fields = ["from_name", "from_email", "reply_to", "signature_text", "signature_html", "use_signature", "include_logo_in_signature", "legal_footer"]
    save_email_section(db, settings, data, user, fields)
    log_action(db, company_id=user.company_id, user=user, action="settings.email.signature.update", entity_type="settings", entity_id=settings.id, message="Firma de correo actualizada")
    return redirect_or_json(request, {"ok": True, "signature": serialize_email_settings(db, user.company_id)["signature"]}, "email-signature")


@router.get("/branding")
def get_branding(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(branding_to_dict(get_or_create_branding(db, user.company_id)))


@router.put("/branding")
async def put_branding(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name != "Administrador":
        return JSONResponse({"error": "Solo Administrador puede modificar identidad corporativa."}, status_code=403)
    data = await request.json()
    branding = get_or_create_branding(db, user.company_id)
    for field in ["company_name", "app_name", "primary_claim", "secondary_claim", "short_description", "logo_url", "dark_logo_url", "favicon_url"]:
        if field in data:
            setattr(branding, field, data[field])
    if "theme" in data:
        import json
        branding.theme_json = json.dumps(data["theme"])
    if "microcopy" in data:
        import json
        branding.microcopy_json = json.dumps(data["microcopy"])
    branding.updated_by = resolve_updated_by_id(db, user)
    db.commit()
    invalidate_branding_cache(user.company_id)
    log_action(db, company_id=user.company_id, user=user, action="branding.update", entity_type="branding", entity_id=branding.id, message="Identidad corporativa actualizada via API")
    return JSONResponse(branding_to_dict(branding))


@router.post("/branding")
async def update_branding(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name != "Administrador":
        log_action(db, company_id=user.company_id, user=user, action="branding.update.denied", entity_type="branding", message="Intento de modificar identidad corporativa sin permisos")
        return RedirectResponse("/settings", status_code=303)
    formdata = await request.form()
    form = {key: value for key, value in formdata.items() if not isinstance(value, UploadFile)}
    branding = get_or_create_branding(db, user.company_id)
    update_branding_from_form(branding, form, resolve_updated_by_id(db, user))
    for field, upload_key, remove_key, prefix in [
        ("logo_url", "logo_file", "remove_logo", "logo-main"),
        ("dark_logo_url", "dark_logo_file", "remove_dark_logo", "logo-dark"),
        ("favicon_url", "favicon_file", "remove_favicon", "favicon"),
    ]:
        upload = formdata.get(upload_key)
        if form.get(remove_key) == "on":
            delete_brand_asset(getattr(branding, field))
            setattr(branding, field, None)
        elif isinstance(upload, UploadFile) and upload.filename:
            delete_brand_asset(getattr(branding, field))
            setattr(branding, field, await store_brand_asset(user.company_id, upload, prefix))
    db.commit()
    invalidate_branding_cache(user.company_id)
    log_action(db, company_id=user.company_id, user=user, action="branding.update", entity_type="branding", entity_id=branding.id, message="Identidad corporativa actualizada")
    return RedirectResponse("/settings#branding", status_code=303)


@router.post("/branding/reset-default")
def reset_branding_default(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name == "Administrador":
        branding = get_or_create_branding(db, user.company_id)
        reset_branding(branding, resolve_updated_by_id(db, user))
        db.commit()
        invalidate_branding_cache(user.company_id)
        log_action(db, company_id=user.company_id, user=user, action="branding.reset_default", entity_type="branding", entity_id=branding.id, message="Identidad corporativa restaurada a valores por defecto")
    return RedirectResponse("/settings#branding", status_code=303)


@router.post("/company/reset-default")
def reset_company_default(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name != "Administrador":
        return RedirectResponse("/settings#general", status_code=303)
    settings = get_settings()
    company = db.get(Company, user.company_id)
    if company:
        delete_brand_asset(company.logo_url)
        company.name = settings.default_company_name
        company.legal_name = None
        company.tax_id = None
        company.email = None
        company.phone = None
        company.web = None
        company.address = None
        company.country = None
        company.currency = "EUR"
        company.notification_email = None
        company.responsible_contact = None
        company.active = True
        company.logo_url = None
        company.language = "es"
        company.timezone = "Europe/Madrid"
        company.date_format = "%d/%m/%Y"
        company.decimal_separator = ","
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="company.reset_default", entity_type="company", entity_id=company.id, message="Configuracion general restaurada a valores por defecto")
    return RedirectResponse("/settings#general", status_code=303)


@router.post("/{section}")
async def update_settings(section: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    anchor = await update_settings_section_async(section, request, db, user)
    log_action(db, company_id=user.company_id, user=user, action=f"settings.{section}.update", entity_type="settings", message=f"Configuracion actualizada: {section}")
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


@router.post("/test/{section}")
def test_connection(section: str, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    result, anchor = run_connection_test(section, db, user)
    log_action(db, company_id=user.company_id, user=user, action=f"settings.{section}.test", entity_type="settings", message=result["message"])
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


@router.post("/email/imap/test")
def test_email_imap(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    request_id = getattr(request.state, "request_id", None)
    provider_name = (settings.provider or "imap").strip().lower()
    host_name = (settings.imap_host or "").strip()
    port_number = settings.imap_port
    security_name = (settings.imap_security or "").strip().lower()
    try:
        result = test_imap_connection(settings, request_id=request_id)
        settings.last_imap_test_at = datetime.now(timezone.utc)
        settings.last_imap_test_ok = result["ok"]
        settings.last_imap_test_message = result["message"]
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="email.imap.test", entity_type="settings", entity_id=settings.id, message=result["message"])
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "imap.test.route_failed",
            extra={
                "event": "imap.test.route_failed",
                "request_id": request_id,
                "provider": provider_name,
                "host": host_name,
                "port": port_number,
                "security": security_name,
                "error_type": exc.__class__.__name__,
            },
        )
        settings.last_imap_test_at = datetime.now(timezone.utc)
        settings.last_imap_test_ok = False
        settings.last_imap_test_message = "No se ha podido comprobar la conexión IMAP en este momento."
        try:
            db.commit()
        except Exception:
            db.rollback()
        try:
            log_action(db, company_id=user.company_id, user=user, action="email.imap.test", entity_type="settings", entity_id=settings.id, message=settings.last_imap_test_message)
        except Exception:
            db.rollback()
    return RedirectResponse("/settings#email-diagnostics", status_code=303)


@router.post("/email/smtp/test")
def test_email_smtp(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    result = test_smtp_connection(settings)
    settings.last_smtp_test_at = datetime.now(timezone.utc)
    settings.last_smtp_test_ok = result["ok"]
    settings.last_smtp_test_message = result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="email.smtp.test", entity_type="settings", entity_id=settings.id, message=result["message"])
    return RedirectResponse("/settings#email-diagnostics", status_code=303)


@router.post("/email/smtp/send-test")
def send_email_test(to_email: str = Form(...), subject: str = Form("Prueba SMTP"), message: str = Form("Correo de prueba enviado desde Anchi."), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    result = send_test_email(settings, to_email, subject, message)
    settings.last_smtp_test_at = datetime.now(timezone.utc)
    settings.last_smtp_test_ok = result["ok"]
    settings.last_smtp_test_message = result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="email.smtp.send_test", entity_type="settings", entity_id=settings.id, message=result["message"])
    return RedirectResponse("/settings#email-diagnostics", status_code=303)


@router.post("/email/read")
def read_email(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.read.start",
        extra={"event": "settings.email.read.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id},
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    safe_limit = max(min(int(settings.read_limit or 10), 50), 1)
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"auto_process": False, "unread_only": False, "limit": safe_limit}, created_by_user_id=user.id)
    logger.info(
        "settings.email.read.requested",
        extra={"event": "settings.email.read.requested", "request_id": request_id, "company_id": user.company_id, "job_id": job.id, "job_type": job.job_type},
    )
    result = _run_email_sync_job_if_needed(request, db, user, job)
    if result is not None:
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="email.read.inline",
            entity_type="job",
            entity_id=job.id,
            message=result.get("message") or "Lectura IMAP completada",
        )
        return _queued_job_response(request, job.id, result=result)
    log_action(db, company_id=user.company_id, user=user, action="email.read", entity_type="job", entity_id=job.id, message="Lectura IMAP solicitada")
    return _queued_job_response(request, job.id)


@router.post("/email/read-unprocessed")
def read_unprocessed_email(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.read_unprocessed.start",
        extra={"event": "settings.email.read_unprocessed.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id},
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    safe_limit = max(min(int(settings.read_limit or 10), 50), 1)
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"auto_process": False, "unread_only": True, "limit": safe_limit}, created_by_user_id=user.id)
    logger.info(
        "settings.email.read_unprocessed.requested",
        extra={"event": "settings.email.read_unprocessed.requested", "request_id": request_id, "company_id": user.company_id, "job_id": job.id, "job_type": job.job_type},
    )
    result = _run_email_sync_job_if_needed(request, db, user, job)
    if result is not None:
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="email.read_unprocessed.inline",
            entity_type="job",
            entity_id=job.id,
            message=result.get("message") or "Lectura de correos recientes completada",
        )
        return _queued_job_response(request, job.id, "/settings#email-diagnostics", result=result)
    log_action(db, company_id=user.company_id, user=user, action="email.read_unprocessed", entity_type="job", entity_id=job.id, message="Lectura de correos recientes solicitada")
    return _queued_job_response(request, job.id, "/settings#email-diagnostics")


@router.post("/email/backfill")
def backfill_email_history(
    request: Request,
    from_date: str = Form(""),
    to_date: str = Form(""),
    limit: int = Form(100),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    if from_date:
        settings.read_from_date = from_date
    from_date_value = _parse_date_input(from_date or settings.read_from_date)
    to_date_value = _parse_date_input(to_date)
    if from_date_value and to_date_value and to_date_value < from_date_value:
        return JSONResponse({"ok": False, "message": "La fecha final no puede ser anterior a la inicial."}, status_code=400)
    safe_limit = max(min(int(limit or 100), 100), 1)
    run_id = getattr(request.state, "request_id", None)

    payload = {
        "from_date": from_date or settings.read_from_date,
        "to_date": to_date or None,
        "limit": safe_limit,
    }
    if run_id:
        payload["run_id"] = run_id

    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="backfill_imap",
        payload=payload,
        created_by_user_id=user.id,
    )
    db.commit()
    result = _run_backfill_job_once(
        request,
        db,
        user,
        job,
        from_date=payload.get("from_date"),
        to_date=payload.get("to_date"),
    )
    date_label = payload.get("from_date") or "configuración"
    if payload.get("to_date"):
        date_label = f"{date_label} a {payload.get('to_date')}"
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="email.backfill",
        entity_type="job",
        entity_id=job.id,
        message=(
            f"Backfill IMAP ejecutado desde {date_label} (límite {safe_limit}) · "
            f"lotes={result.get('batches')} · guardados={result.get('saved')} · "
            f"duplicados={result.get('duplicates')} · errores={result.get('errors')}"
        ),
    )
    return _backfill_response(request, result, "/settings#email-diagnostics")


@router.post("/email/backfill/continue/{job_id}")
def continue_backfill_email_history(
    job_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not can_test_email_settings(user):
        return JSONResponse({"ok": False, "error_type": "permission_denied", "message": "No tienes permisos para continuar el backfill histórico."}, status_code=403)
    job = db.get(BackgroundJob, job_id)
    if not job or job.company_id != user.company_id:
        return JSONResponse({"ok": False, "error_type": "job_not_found", "message": "No se encontró la continuación solicitada."}, status_code=404)
    if job.job_type != "backfill_imap":
        return JSONResponse({"ok": False, "error_type": "invalid_job_type", "message": "La continuación solicitada no corresponde a un backfill IMAP."}, status_code=400)
    if job.status not in {"queued", "retrying"}:
        return JSONResponse({"ok": False, "error_type": "invalid_job_status", "message": "La continuación del backfill no está lista para ejecutarse."}, status_code=409)
    payload = job_payload(job)
    result = _run_backfill_job_once(
        request,
        db,
        user,
        job,
        from_date=payload.get("from_date"),
        to_date=payload.get("to_date"),
    )
    return _backfill_response(request, result, "/settings#email-diagnostics")


@router.post("/email/initial-sync/preview")
async def preview_email_initial_sync(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-receive", status_code=303)
    data = _normalize_receive_form(await request_data(request))
    preview_settings = _clone_email_settings_for_preview(db, user.company_id, data)
    preview = preview_initial_imap_sync(preview_settings)
    return templates.TemplateResponse(
        "settings/email_initial_sync_preview.html",
        {
            "request": request,
            "user": user,
            "company": db.get(Company, user.company_id),
            "preview": preview,
            "form_data": data,
            "can_edit_email": can_edit_email_settings(user),
        },
    )


@router.post("/email/initial-sync")
async def confirm_email_initial_sync(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-receive", status_code=303)
    data = _normalize_receive_form(await request_data(request))
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    save_email_section(db, settings, data, user, ["provider", "imap_host", "imap_port", "imap_security", "imap_use_ssl", "imap_username", "imap_password_encrypted", "inbox_folder", "processed_folder", "error_folder", "no_order_folder", "doubtful_folder", "read_limit", "test_read_limit", "auto_sync_enabled", "read_unread_only", "read_from_date", "initial_history_mode", "initial_history_limit", "mark_as_read_after_import", "move_after_processing", "post_process_action", "polling_frequency_minutes", "client_id", "client_secret_encrypted", "tenant_id", "redirect_uri", "oauth_scopes", "mailbox", "access_token_encrypted", "refresh_token_encrypted", "connected_email"], {"imap_password_encrypted", "client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted"})
    try:
        settings.initial_history_limit = max(min(int(settings.initial_history_limit or 50), 100), 1)
    except (TypeError, ValueError):
        settings.initial_history_limit = 50
    settings.initial_history_mode = settings.initial_history_mode if settings.initial_history_mode in {"new", "7d", "30d", "100", "custom"} else "new"
    db.commit()
    _sync_email_sync_state(master_db, user, settings)
    sync_state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == user.company_id, EmailSyncState.channel_key == "email"))
    try:
        result = run_initial_imap_sync(db, settings, user.company_id, sync_state=sync_state, sync_session=master_db)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    log_action(db, company_id=user.company_id, user=user, action="email.initial_sync", entity_type="settings", entity_id=settings.id, message=result.get("message") or "Sincronizacion inicial ejecutada")
    return redirect_or_json(request, {"ok": result.get("ok", False), "message": result.get("message"), "preview": result}, "email-receive")


@router.get("/agent")
def get_agent_settings(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    metrics = agent_metrics(db, user.company_id, scoring)
    return JSONResponse({"status": agent_status(llm, metrics), "metrics": metrics})


@router.post("/agent/activate")
def activate_agent(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    llm.agent_enabled = True
    if llm.agent_mode == "desactivado":
        llm.agent_mode = "semiautomatico"
    llm.updated_by = resolve_updated_by_id(db, user)
    llm.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.settings_updated", entity_type="settings", entity_id=llm.id, message="Agente IA activado")
    return RedirectResponse("/settings#agent-ai", status_code=303)


@router.post("/agent/pause")
def pause_agent(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    llm.agent_enabled = False
    llm.updated_by = resolve_updated_by_id(db, user)
    llm.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.settings_updated", entity_type="settings", entity_id=llm.id, message="Agente IA pausado")
    return RedirectResponse("/settings#agent-ai", status_code=303)


@router.post("/agent/test-full-flow")
def test_agent_full_flow(
    request: Request,
    sample_text: str = Form(AGENT_FLOW_DEMO_SAMPLE),
    classification_model_mode: str = Form(""),
    classification_model_custom: str = Form(""),
    classification_model: str = Form(""),
    extraction_model_mode: str = Form(""),
    extraction_model_custom: str = Form(""),
    extraction_model: str = Form(""),
    validation_model_mode: str = Form(""),
    validation_model_custom: str = Form(""),
    validation_model: str = Form(""),
    use_same_model_for_all: str = Form(""),
    reasoning_effort: str = Form(""),
    api_key_encrypted: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    # This endpoint is a repeatable model smoke test, so it must not depend on
    # arbitrary browser input or records that may be missing from a tenant.
    sample_text = AGENT_FLOW_DEMO_SAMPLE
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    test_settings = _agent_flow_test_settings(
        llm,
        classification_model_mode=classification_model_mode,
        classification_model_custom=classification_model_custom,
        classification_model=classification_model,
        extraction_model_mode=extraction_model_mode,
        extraction_model_custom=extraction_model_custom,
        extraction_model=extraction_model,
        validation_model_mode=validation_model_mode,
        validation_model_custom=validation_model_custom,
        validation_model=validation_model,
        use_same_model_for_all=use_same_model_for_all,
        reasoning_effort=reasoning_effort,
        api_key_encrypted=api_key_encrypted,
    )
    start = perf_counter()
    classification = _run_agent_flow_step(
        lambda: classify_sample(db, test_settings, user.company_id, sample_text, active_prompt_content(db, user.company_id, "classification")),
        "clasificación",
    )
    extraction = (
        _run_agent_flow_step(
            lambda: extract_sample(db, test_settings, user.company_id, sample_text, active_prompt_content(db, user.company_id, "extraction")),
            "extracción",
        )
        if classification.get("ok")
        else {"ok": False, "message": "No se ejecutó extracción porque falló la clasificación.", "skipped": True}
    )
    validation = (
        _run_agent_flow_step(
            lambda: validate_sample(
                db,
                test_settings,
                user.company_id,
                sample_text,
                extraction.get("validated_content") or {},
                active_prompt_content(db, user.company_id, "validation"),
                AGENT_FLOW_DEMO_VALIDATION_CONTEXT,
            ),
            "validación",
        )
        if extraction.get("ok")
        else {"ok": False, "message": "No se ejecutó validación porque falló la extracción.", "skipped": True}
    )
    elapsed_ms = int((perf_counter() - start) * 1000)
    ok = bool(classification.get("ok") and extraction.get("ok") and validation.get("ok"))
    models = {
        "classification": test_settings.classification_model,
        "extraction": test_settings.extraction_model,
        "validation": test_settings.validation_model,
    }
    steps = [
        _agent_flow_step_payload("Clasificación", classification, models["classification"]),
        _agent_flow_step_payload("Extracción", extraction, models["extraction"]),
        _agent_flow_step_payload("Validación", validation, models["validation"]),
    ]
    failed_messages = [step["message"] for step in steps if not step["ok"] and not step.get("skipped")]
    status_label = "correcto" if ok else "con incidencias"
    model_summary = ", ".join(f"{label}={model}" for label, model in [("clasificación", models["classification"]), ("extracción", models["extraction"]), ("validación", models["validation"])])
    llm.last_test_at = datetime.now(timezone.utc)
    llm.last_test_ok = bool(ok)
    llm.last_test_message = f"Flujo completo {status_label}. Modelos: {model_summary}. Tiempo: {elapsed_ms} ms. No se confirmó ni exportó ningún pedido."
    llm.last_error = None if ok else " ".join(failed_messages) or "La prueba no pudo completar todos los pasos."
    llm.last_response_ms = elapsed_ms
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.process_email", entity_type="settings", entity_id=llm.id, message=llm.last_test_message)
    response_payload = {
        "ok": bool(ok),
        "message": llm.last_test_message,
        "steps": steps,
        "duration_ms": elapsed_ms,
    }
    if request.headers.get("x-requested-with") == "fetch" or "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(response_payload, status_code=200 if ok else 422)
    return RedirectResponse("/settings#agent-tests", status_code=303)


def _agent_flow_test_settings(
    llm: LLMSettings,
    *,
    classification_model_mode: str,
    classification_model_custom: str,
    classification_model: str,
    extraction_model_mode: str,
    extraction_model_custom: str,
    extraction_model: str,
    validation_model_mode: str,
    validation_model_custom: str,
    validation_model: str,
    use_same_model_for_all: str,
    reasoning_effort: str,
    api_key_encrypted: str,
) -> SimpleNamespace:
    selected_extraction = resolve_openai_model_choice(
        extraction_model_mode,
        extraction_model_custom,
        extraction_model or resolve_openai_runtime_model(llm.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
    )
    selected_classification = resolve_openai_model_choice(
        classification_model_mode,
        classification_model_custom,
        classification_model or resolve_openai_runtime_model(llm.classification_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
    )
    selected_validation = resolve_openai_model_choice(
        validation_model_mode,
        validation_model_custom,
        validation_model or resolve_openai_runtime_model(llm.validation_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
    )
    if use_same_model_for_all == "on":
        selected_classification = selected_extraction
        selected_validation = selected_extraction

    submitted_api_key = (api_key_encrypted or "").strip()
    if submitted_api_key and submitted_api_key not in {"********", "••••••••"} and decrypt_secret(submitted_api_key) is None:
        submitted_api_key = encrypt_secret(submitted_api_key) or llm.api_key_encrypted
    else:
        submitted_api_key = llm.api_key_encrypted

    normalized_reasoning = (reasoning_effort or getattr(llm, "reasoning_effort", None) or DEFAULT_REASONING_EFFORT).strip().lower()
    if normalized_reasoning not in REASONING_EFFORT_VALUES:
        normalized_reasoning = DEFAULT_REASONING_EFFORT
    return SimpleNamespace(
        provider=llm.provider,
        api_key_encrypted=submitted_api_key,
        base_url=llm.base_url,
        temperature=llm.temperature,
        max_tokens=llm.max_tokens,
        retries=llm.retries,
        timeout_seconds=llm.timeout_seconds,
        classification_model=selected_classification,
        extraction_model=selected_extraction,
        validation_model=selected_validation,
        reasoning_effort=normalized_reasoning,
    )


def _run_agent_flow_step(callback, label: str) -> dict:
    try:
        return callback()
    except Exception as exc:  # pragma: no cover - defensive boundary for provider failures
        logger.exception("Fallo en la prueba de flujo IA", extra={"step": label})
        return {"ok": False, "message": f"{label.capitalize()}: error inesperado ({type(exc).__name__})."}


def _agent_flow_step_payload(label: str, result: dict, fallback_model: str) -> dict:
    return {
        "label": label,
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("skipped")),
        "model": result.get("model") or fallback_model,
        "message": result.get("message") or ("Completado correctamente." if result.get("ok") else "No completado."),
        "duration_ms": result.get("duration_ms"),
    }


@router.post("/llm/classify")
def test_llm_classification(sample_text: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    start = perf_counter()
    prompt = active_prompt_content(db, user.company_id, "classification")
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    result = classify_sample(db, llm, user.company_id, sample_text, prompt)
    llm.last_test_at = datetime.now(timezone.utc)
    llm.last_test_ok = result["ok"]
    llm.last_test_message = f"Clasificacion: {result['message']} Tiempo: {int((perf_counter() - start) * 1000)} ms."
    llm.last_error = None if result["ok"] else result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.classification_test", entity_type="settings", message=llm.last_test_message)
    return RedirectResponse("/settings#agent-tests", status_code=303)


@router.post("/llm/extract")
def test_llm_extraction(sample_text: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    start = perf_counter()
    prompt = active_prompt_content(db, user.company_id, "extraction")
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    result = extract_sample(db, llm, user.company_id, sample_text, prompt)
    llm.last_test_at = datetime.now(timezone.utc)
    llm.last_test_ok = result["ok"]
    llm.last_test_message = f"Extraccion: {result['message']} Tiempo: {int((perf_counter() - start) * 1000)} ms."
    llm.last_error = None if result["ok"] else result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.extraction_test", entity_type="settings", message=llm.last_test_message)
    return RedirectResponse("/settings#agent-tests", status_code=303)


def active_prompt_content(db: Session, company_id: int, purpose: str) -> str:
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
    if not template or not template.active_version_id:
        return DEFAULT_AGENT_PROMPTS.get(purpose, "Responde en JSON valido cuando se solicite extraccion.")
    version = db.get(PromptVersion, template.active_version_id)
    return version.content if version else DEFAULT_AGENT_PROMPTS.get(purpose, "Responde en JSON valido cuando se solicite extraccion.")


DEFAULT_AGENT_PROMPTS = {
    "classification": "Clasifica el correo como pedido, no_pedido, consulta, incidencia o dudoso. Responde JSON valido con tipo_correo, confianza y motivo.",
    "extraction": "Extrae un pedido en JSON valido con cliente, fechas, observaciones y lineas con producto, referencia, cantidad y unidad.",
    "validation": "Valida el pedido extraido contra datos de cliente y producto. Devuelve JSON con advertencias y bloqueos como listas de textos u objetos con tipo, campo y mensaje, además de scoring recomendado.",
    "non_order": "Resume por que el correo no contiene pedido y clasificalo como consulta, incidencia, no_pedido o dudoso.",
    "doubtful": "Analiza un correo dudoso y devuelve JSON con motivos de duda, campos ambiguos y accion recomendada.",
}


@router.post("/prompts/reset-defaults")
def reset_agent_prompts(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name == "Administrador":
        for purpose, content in DEFAULT_AGENT_PROMPTS.items():
            template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == user.company_id, PromptTemplate.purpose == purpose))
            if not template:
                template = PromptTemplate(company_id=user.company_id, name=purpose.replace("_", " ").title(), purpose=purpose)
                db.add(template)
                db.flush()
            last_version = db.scalar(select(PromptVersion.version).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())) or 0
            version = PromptVersion(company_id=user.company_id, template_id=template.id, version=last_version + 1, content=content, created_by_user_id=user.id)
            db.add(version)
            db.flush()
            template.active_version_id = version.id
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="agent.prompt_updated", entity_type="prompt", message="Prompts restaurados a valores por defecto")
    return RedirectResponse("/settings#agent-prompts", status_code=303)


@router.post("/prompts/{template_id}/duplicate")
def duplicate_prompt(template_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    template = db.get(PromptTemplate, template_id)
    if template and template.company_id == user.company_id and user.role.name == "Administrador":
        active = db.get(PromptVersion, template.active_version_id) if template.active_version_id else None
        if active:
            last_version = db.scalar(select(PromptVersion.version).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())) or 0
            version = PromptVersion(company_id=user.company_id, template_id=template.id, version=last_version + 1, content=active.content, created_by_user_id=user.id)
            db.add(version)
            db.commit()
            log_action(db, company_id=user.company_id, user=user, action="agent.prompt_updated", entity_type="prompt", entity_id=template.id, message=f"Prompt duplicado: {template.purpose}")
    return RedirectResponse("/settings#agent-prompts", status_code=303)


@router.post("/prompts/{template_id}")
def save_prompt(template_id: int, content: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    template = db.get(PromptTemplate, template_id)
    if template and template.company_id == user.company_id:
        last_version = db.scalar(select(PromptVersion.version).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())) or 0
        version = PromptVersion(company_id=user.company_id, template_id=template.id, version=last_version + 1, content=content, created_by_user_id=user.id)
        db.add(version)
        db.flush()
        template.active_version_id = version.id
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="agent.prompt_updated", entity_type="prompt", entity_id=template.id, message=f"Prompt actualizado: {template.purpose}")
    return RedirectResponse("/settings#agent-prompts", status_code=303)
