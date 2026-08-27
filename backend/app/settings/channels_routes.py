from __future__ import annotations

from datetime import datetime, timezone
import hmac
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.templating import templates
from app.core.timezones import format_local_datetime
from app.db.models import ChannelSetting, Company, EmailSettings, InputChannel
from app.dashboard.service import recent_processed_emails_overview
from app.logs.service import log_action
from app.master.database import get_master_db
from app.master.models import EmailSyncState
from app.master.service import TenantUser
from app.settings.email_config import email_config_status
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import (
    WhatsAppEmbeddedSignupError,
    complete_embedded_signup,
    embedded_signup_public_config,
    get_or_create_whatsapp_channel,
    redact_whatsapp_config,
    whatsapp_config,
    whatsapp_webhook_url,
)

router = APIRouter()


CHANNEL_CONFIG_SPECS = {
    "email": {
        "summary": "El canal principal sigue reutilizando la configuración de Correo.",
        "config_title": "Correo conectado",
        "fields": [],
        "requires_email_settings": True,
        "test_label": "Probar IMAP",
        "configure_href": "/settings#email",
    },
    "whatsapp": {
        "summary": "Conecta WhatsApp Business mediante el acceso seguro de Meta.",
        "config_title": "WhatsApp Business Platform",
        "fields": [],
        "required_keys": ["phone_number_id", "business_account_id", "access_token", "verify_token"],
    },
    "voice": {
        "summary": "Pensado para voz y transcripción posterior.",
        "config_title": "Teléfono / voz",
        "fields": [{"key": "display_name", "label": "Nombre visible"}, {"key": "phone_number", "label": "Número de línea"}, {"key": "transcription_provider", "label": "Proveedor transcripción"}, {"key": "language", "label": "Idioma"}],
        "required_keys": ["phone_number", "transcription_provider"],
    },
    "social": {
        "summary": "Ideal para mensajería social y adjuntos ligeros.",
        "config_title": "Redes sociales",
        "fields": [{"key": "display_name", "label": "Nombre visible"}, {"key": "platform", "label": "Plataforma"}, {"key": "account_id", "label": "Cuenta / página"}, {"key": "access_token", "label": "Token de acceso", "secret": True}, {"key": "webhook_secret", "label": "Secreto webhook", "secret": True}],
        "required_keys": ["platform", "account_id", "access_token"],
    },
}


def has_admin_access(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


def channel_capabilities(channel: InputChannel) -> list[str]:
    capabilities = []
    if channel.supports_text:
        capabilities.append("Texto")
    if channel.supports_attachments:
        capabilities.append("Adjuntos")
    if getattr(channel, "supports_images", False):
        capabilities.append("Imágenes")
    if channel.supports_audio:
        capabilities.append("Audio")
    if channel.supports_documents:
        capabilities.append("Documentos")
    return capabilities


def _sync_email_channel_state(
    master_db: Session,
    db: Session,
    company_id: int,
    *,
    active: bool,
) -> None:
    settings = get_or_create_settings(db, EmailSettings, company_id)
    state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == company_id,
            EmailSyncState.channel_key == "email",
        )
    )

    should_enable = bool(active and settings.auto_sync_enabled)
    frequency_seconds = max(
        int(settings.polling_frequency_minutes or 1),
        1,
    ) * 60

    if not state:
        state = EmailSyncState(
            company_id=company_id,
            channel_key="email",
            enabled=should_enable,
            frequency_seconds=frequency_seconds,
            status="idle",
            listener_status="inactive",
            next_run_at=datetime.now(timezone.utc) if should_enable else None,
        )
        master_db.add(state)
    else:
        state.enabled = should_enable
        state.frequency_seconds = frequency_seconds
        state.next_run_at = datetime.now(timezone.utc) if should_enable else None
        if not active:
            state.listener_status = "inactive"

    master_db.commit()


def channel_settings_map(db: Session, company_id: int, channel_id: int) -> dict[str, str | None]:
    settings = db.scalars(select(ChannelSetting).where(ChannelSetting.company_id == company_id, ChannelSetting.channel_id == channel_id)).all()
    return {setting.key: setting.value for setting in settings}


def _get_channel_or_404(db: Session, company_id: int, channel_key: str) -> InputChannel | None:
    return db.scalar(select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.key == channel_key))


def _upsert_channel_setting(db: Session, company_id: int, channel_id: int, key: str, value: str | None, value_type: str = "string", is_secret: bool = False) -> None:
    setting = db.scalar(select(ChannelSetting).where(ChannelSetting.company_id == company_id, ChannelSetting.channel_id == channel_id, ChannelSetting.key == key))
    if value is None or value == "":
        if setting:
            db.delete(setting)
        return
    if not setting:
        setting = ChannelSetting(company_id=company_id, channel_id=channel_id, key=key)
        db.add(setting)
    setting.value = value
    setting.value_type = value_type
    setting.is_secret = is_secret
    setting.updated_at = datetime.now(timezone.utc)


def channel_status_payload(db: Session, company_id: int, channel: InputChannel) -> dict:
    spec = CHANNEL_CONFIG_SPECS.get(channel.key, {})
    settings_map = channel_settings_map(db, company_id, channel.id)
    email_status = email_config_status(get_or_create_settings(db, EmailSettings, company_id)) if channel.key == "email" else None
    company = db.get(Company, company_id)
    timezone_name = company.timezone if company else None
    state = "inactive"
    status_label = "Inactivo"
    details = spec.get("summary", "Canal preparado para activarse por cliente.")
    activity_label = "Sin actividad"
    activity_value = "No se han recibido entradas todavía."
    if channel.is_active:
        if channel.key == "email":
            has_error = bool(email_status["last_sync"]["error"])
            if has_error:
                state = "error"
                status_label = "Activo · con error"
                details = email_status["last_sync"]["error"] or email_status["last_imap_test"]["message"] or email_status["last_smtp_test"]["message"] or "Revisar configuración de correo."
                activity_label = "Último error"
                activity_value = details
            elif email_status["imap_ready"]:
                state = "ready"
                status_label = "Activo · configurado"
                last_sync = email_status["last_sync"]
                if last_sync["at"]:
                    activity_label = "Última lectura"
                    activity_value = f"{format_local_datetime(last_sync['at'], timezone_name, '%d/%m %H:%M', 'Sin lectura reciente')} · {last_sync['new']} nuevos"
                else:
                    activity_label = "Última lectura"
                    activity_value = "Sin lectura reciente"
                details = "Correo listo para recibir pedidos. El envío SMTP es opcional."
            else:
                state = "pending"
                status_label = "Activo · pendiente de configurar"
                details = "Faltan datos para empezar a recibir mensajes."
                activity_label = "Pendiente"
                activity_value = "Completa los datos mínimos del correo."
        elif channel.key == "whatsapp":
            connection_status = (settings_map.get("connection_status") or "not_connected").strip().lower()
            required_ready = all(settings_map.get(key) for key in spec.get("required_keys", []))
            if connection_status == "error":
                state = "error"
                status_label = "Activo · error de conexión"
                details = settings_map.get("last_error") or "Meta no pudo completar la conexión. Vuelve a iniciar sesión."
                activity_label = "Conexión"
                activity_value = "Requiere atención"
            elif connection_status == "connected" and required_ready and settings_map.get("webhook_enabled") == "true":
                state = "ready"
                status_label = "Activo · conectado con Meta"
                details = "WhatsApp Business está suscrito al webhook de este tenant."
                activity_label = "Número conectado"
                activity_value = settings_map.get("display_phone_number") or settings_map.get("phone_number_id") or "Conectado"
            else:
                state = "pending"
                status_label = "Activo · pendiente de conectar"
                details = "Inicia sesión con Meta para autorizar el WABA y registrar el número."
                activity_label = "Pendiente"
                activity_value = "Completa Embedded Signup"
        else:
            missing = [field["label"] for field in spec.get("fields", []) if field["key"] in spec.get("required_keys", []) and not settings_map.get(field["key"])]
            if missing:
                state = "pending"
                status_label = "Activo · pendiente de configurar"
                details = "Faltan datos para empezar a recibir mensajes."
                activity_label = "Pendiente"
                activity_value = " · ".join(missing[:3])
            else:
                state = "ready"
                status_label = "Activo · configurado"
                details = "Canal operativo con la configuración actual."
                activity_label = "Última actividad"
                activity_value = "Sin actividad todavía"
    return {"state": state, "status_label": status_label, "details": details, "activity_label": activity_label, "activity_value": activity_value, "settings": settings_map, "email_status": email_status, "spec": spec}


def channel_settings_overview(db: Session, company_id: int) -> list[dict]:
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.is_default.desc(), InputChannel.name)).all()
    rows = []
    for channel in channels:
        payload = channel_status_payload(db, company_id, channel)
        rows.append({"channel": channel, "raw_settings": [], "settings_count": 0, "capabilities": channel_capabilities(channel), **payload})
    return rows


def channel_settings_overview_fallback(db: Session, company_id: int) -> list[dict]:
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.is_default.desc(), InputChannel.name)).all()
    return [{"channel": channel, "raw_settings": [], "settings_count": 0, "capabilities": channel_capabilities(channel), "state": "inactive", "status_label": "Inactivo", "details": "No se pudo leer el detalle de configuración.", "activity_label": "Sin actividad", "activity_value": "Estado temporalmente no disponible.", "settings": {}, "email_status": email_config_status(get_or_create_settings(db, EmailSettings, company_id)) if channel.key == "email" else None, "spec": CHANNEL_CONFIG_SPECS.get(channel.key, {})} for channel in channels]


@router.get("/settings/channels")
def channels_settings_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/", status_code=303)
    get_or_create_whatsapp_channel(db, user.company_id)
    try:
        overview = channel_settings_overview(db, user.company_id)
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="channel.settings_overview_error", entity_type="input_channel", message=f"Error al construir la vista de canales: {exc}")
        overview = channel_settings_overview_fallback(db, user.company_id)
    technical_access = user.role.name == "Superadmin"
    focus_key = request.query_params.get("focus")
    whatsapp_signup_state = secrets.token_urlsafe(32)
    request.session["whatsapp_embedded_signup_state"] = whatsapp_signup_state
    return templates.TemplateResponse(
        "settings/channels.html",
        {
            "request": request,
            "user": user,
            "channels": overview,
            "recent_processed_emails": recent_processed_emails_overview(db, user.company_id, days=30, limit=8),
            "email": get_or_create_settings(db, EmailSettings, user.company_id),
            "whatsapp": redact_whatsapp_config(whatsapp_config(db, user.company_id)),
            "whatsapp_embedded_signup": embedded_signup_public_config(),
            "whatsapp_signup_state": whatsapp_signup_state,
            "webhook_url": whatsapp_webhook_url(user.company_slug),
            "technical_access": technical_access,
            "active_channels": [channel for channel in overview if channel["channel"].is_active],
            "focus_key": focus_key,
        },
    )


@router.post("/settings/channels/whatsapp/embedded-signup/complete")
async def complete_whatsapp_embedded_signup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not has_admin_access(user):
        return JSONResponse({"ok": False, "message": "Solo un administrador puede conectar WhatsApp."}, status_code=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "La respuesta de Meta no es válida."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "message": "La respuesta de Meta no es válida."}, status_code=400)
    expected_state = str(request.session.get("whatsapp_embedded_signup_state") or "")
    received_state = str(payload.get("state") or "")
    if not expected_state or not received_state or not hmac.compare_digest(expected_state, received_state):
        return JSONResponse({"ok": False, "message": "La sesión de conexión ha caducado. Recarga la página."}, status_code=403)
    try:
        result = await complete_embedded_signup(
            db,
            company_id=user.company_id,
            company_slug=user.company_slug,
            code=str(payload.get("code") or ""),
            business_account_id=str(payload.get("waba_id") or ""),
            phone_number_id=str(payload.get("phone_number_id") or ""),
            business_id=str(payload.get("business_id") or ""),
            onboarding_mode=str(payload.get("onboarding_mode") or "cloud_api"),
        )
    except WhatsAppEmbeddedSignupError as exc:
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="whatsapp.embedded_signup.failed",
            entity_type="input_channel",
            message=f"Embedded Signup falló: {exc.error_type}",
        )
        status_code = 502
        if exc.error_type == "server_not_configured":
            status_code = 503
        elif exc.error_type in {"invalid_signup_payload", "asset_mismatch"}:
            status_code = 400
        return JSONResponse({"ok": False, "message": str(exc), "error_type": exc.error_type}, status_code=status_code)
    request.session.pop("whatsapp_embedded_signup_state", None)
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="whatsapp.embedded_signup.connected",
        entity_type="input_channel",
        message="WhatsApp conectado mediante Embedded Signup",
    )
    return_to = "setup" if payload.get("return_to") == "setup" else "settings"
    redirect_url = "/setup/channels?whatsapp=connected" if return_to == "setup" else "/settings/channels?focus=whatsapp&whatsapp=connected"
    return JSONResponse(
        {
            "ok": True,
            "message": "WhatsApp se ha conectado correctamente.",
            "connection": {
                "waba_id": result.business_account_id,
                "phone_number_id": result.phone_number_id,
                "display_phone_number": result.display_phone_number,
                "verified_name": result.verified_name,
                "onboarding_mode": result.onboarding_mode,
                "is_on_biz_app": result.is_on_biz_app,
            },
            "redirect_url": redirect_url,
        }
    )


@router.post("/settings/channels/{channel_key}/activate")
async def activate_channel(
    channel_key: str,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    if not has_admin_access(user):
        return RedirectResponse("/settings/channels", status_code=303)
    channel = _get_channel_or_404(db, user.company_id, channel_key)
    if not channel:
        return RedirectResponse("/settings/channels", status_code=303)

    channel.is_active = True
    channel.updated_at = datetime.now(timezone.utc)
    db.commit()

    if channel.key == "email":
        _sync_email_channel_state(
            master_db,
            db,
            user.company_id,
            active=True,
        )

    log_action(db, company_id=user.company_id, user=user, action="channel.activate", entity_type="input_channel", entity_id=channel.id, message=f"Canal activado: {channel.name}")
    return RedirectResponse(f"/settings/channels?focus={channel.key}", status_code=303)


@router.post("/settings/channels/{channel_key}/deactivate")
async def deactivate_channel(
    channel_key: str,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    if not has_admin_access(user):
        return RedirectResponse("/settings/channels", status_code=303)
    channel = _get_channel_or_404(db, user.company_id, channel_key)
    if not channel:
        return RedirectResponse("/settings/channels", status_code=303)

    channel.is_active = False
    channel.updated_at = datetime.now(timezone.utc)
    db.commit()

    if channel.key == "email":
        _sync_email_channel_state(
            master_db,
            db,
            user.company_id,
            active=False,
        )

    log_action(db, company_id=user.company_id, user=user, action="channel.deactivate", entity_type="input_channel", entity_id=channel.id, message=f"Canal desactivado: {channel.name}")
    return RedirectResponse("/settings/channels", status_code=303)


@router.post("/settings/channels/{channel_key}/settings")
async def update_channel_settings(channel_key: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/settings/channels", status_code=303)
    channel = _get_channel_or_404(db, user.company_id, channel_key)
    if not channel:
        return RedirectResponse("/settings/channels", status_code=303)
    if channel.key == "whatsapp":
        return RedirectResponse("/settings/channels?focus=whatsapp", status_code=303)
    form = dict(await request.form())
    spec = CHANNEL_CONFIG_SPECS.get(channel.key, {})
    if channel.key == "email":
        for key in ["display_name", "routing_note", "notes"]:
            _upsert_channel_setting(db, user.company_id, channel.id, key, form.get(key))
    else:
        for field in spec.get("fields", []):
            if field.get("secret") and not form.get(field["key"]):
                continue
            _upsert_channel_setting(db, user.company_id, channel.id, field["key"], form.get(field["key"]), is_secret=field.get("secret", False))
    channel.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="channel.settings.update", entity_type="input_channel", entity_id=channel.id, message=f"Configuración actualizada: {channel.name}")
    return RedirectResponse(f"/settings/channels?focus={channel.key}", status_code=303)
