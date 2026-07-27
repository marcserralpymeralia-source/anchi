from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.templating import templates
from app.db.models import ChannelSetting, EmailSettings, InputChannel
from app.dashboard.service import recent_processed_emails_overview
from app.logs.service import log_action
from app.master.service import TenantUser
from app.settings.email_config import email_config_status
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db
from app.workbench.routes import _redirect_back

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
        "summary": "Preparado para conectarse con un proveedor de mensajería.",
        "config_title": "WhatsApp",
        "fields": [
            {"key": "enabled", "label": "Activo"},
            {"key": "provider", "label": "Proveedor"},
            {"key": "phone_number_id", "label": "Phone number ID"},
            {"key": "business_account_id", "label": "Business account ID"},
            {"key": "access_token", "label": "Token de acceso", "secret": True},
            {"key": "verify_token", "label": "Verify token", "secret": True},
            {"key": "app_secret", "label": "App secret", "secret": True},
            {"key": "webhook_enabled", "label": "Webhook activo"},
            {"key": "bot_enabled", "label": "Bot activo"},
            {"key": "default_language", "label": "Idioma"},
            {"key": "timezone", "label": "Zona horaria"},
        ],
        "required_keys": ["phone_number_id", "business_account_id", "access_token", "verify_token", "app_secret"],
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
                    activity_value = f"{last_sync['at'].strftime('%d/%m %H:%M')} · {last_sync['new']} nuevos"
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
    try:
        overview = channel_settings_overview(db, user.company_id)
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="channel.settings_overview_error", entity_type="input_channel", message=f"Error al construir la vista de canales: {exc}")
        overview = channel_settings_overview_fallback(db, user.company_id)
    technical_access = user.role.name == "Superadmin"
    focus_key = request.query_params.get("focus")
    return templates.TemplateResponse("settings/channels.html", {"request": request, "user": user, "channels": overview, "recent_processed_emails": recent_processed_emails_overview(db, user.company_id, days=30, limit=8), "email": get_or_create_settings(db, EmailSettings, user.company_id), "technical_access": technical_access, "active_channels": [channel for channel in overview if channel["channel"].is_active], "focus_key": focus_key})


@router.post("/settings/channels/{channel_key}/activate")
async def activate_channel(channel_key: str, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/settings/channels", status_code=303)
    channel = _get_channel_or_404(db, user.company_id, channel_key)
    if not channel:
        return RedirectResponse("/settings/channels", status_code=303)
    channel.is_active = True
    channel.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="channel.activate", entity_type="input_channel", entity_id=channel.id, message=f"Canal activado: {channel.name}")
    return RedirectResponse(f"/settings/channels?focus={channel.key}", status_code=303)


@router.post("/settings/channels/{channel_key}/deactivate")
async def deactivate_channel(channel_key: str, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/settings/channels", status_code=303)
    channel = _get_channel_or_404(db, user.company_id, channel_key)
    if not channel:
        return RedirectResponse("/settings/channels", status_code=303)
    channel.is_active = False
    channel.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="channel.deactivate", entity_type="input_channel", entity_id=channel.id, message=f"Canal desactivado: {channel.name}")
    return RedirectResponse("/settings/channels", status_code=303)


@router.post("/settings/channels/{channel_key}/settings")
async def update_channel_settings(channel_key: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/settings/channels", status_code=303)
    channel = _get_channel_or_404(db, user.company_id, channel_key)
    if not channel:
        return RedirectResponse("/settings/channels", status_code=303)
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
