from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agent.platform import LearningService
from app.core.timezones import format_local_datetime
from app.db.models import (
    Alert,
    AuditLog,
    ChannelSetting,
    Company,
    Customer,
    CustomerAlias,
    CustomerDomain,
    Email,
    EmailAttachment,
    EmailSettings,
    InboundMessage,
    ImportJob,
    InputChannel,
    LearnedAlias,
    LLMSettings,
    ManualCorrection,
    Order,
    Product,
    PromptTemplate,
    RagCase,
    RagDocument,
    ScoringSettings,
    User,
)
from app.settings.email_config import email_config_status
from app.settings.service import get_or_create_settings
from app.master.service import TenantUser


def seed_input_channels(db: Session, company_id: int) -> None:
    channel_definitions = [
        {"key": "email", "name": "Email", "is_active": True, "is_default": True, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": True},
        {"key": "whatsapp", "name": "WhatsApp", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": True, "supports_documents": True, "supports_images": False},
        {"key": "voice", "name": "Teléfono / voz", "is_active": False, "is_default": False, "supports_text": False, "supports_attachments": False, "supports_audio": True, "supports_documents": False, "supports_images": False},
        {"key": "social", "name": "Redes sociales", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": False, "supports_images": True},
    ]
    for definition in channel_definitions:
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.key == definition["key"]))
        if channel:
            if not channel.name:
                channel.name = definition["name"]
            channel.supports_text = definition["supports_text"]
            channel.supports_attachments = definition["supports_attachments"]
            channel.supports_audio = definition["supports_audio"]
            channel.supports_documents = definition["supports_documents"]
            if "supports_images" in definition:
                channel.supports_images = definition["supports_images"]
            continue
        db.add(InputChannel(company_id=company_id, **definition))


def _format_agent_time(value: datetime | None) -> str:
    if not value:
        return "Sin lectura registrada"
    return format_local_datetime(value, fmt="%H:%M", default="Sin lectura registrada")


def active_channels_for_company(db: Session, company_id: int) -> list[dict]:
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.is_default.desc(), InputChannel.name)).all()
    return [
        {
            "id": channel.id,
            "key": channel.key,
            "name": channel.name,
            "is_active": channel.is_active,
            "is_default": channel.is_default,
            "supports_text": channel.supports_text,
            "supports_attachments": channel.supports_attachments,
            "supports_audio": channel.supports_audio,
            "supports_documents": channel.supports_documents,
            "supports_images": getattr(channel, "supports_images", False),
        }
        for channel in channels
    ]


def agent_operational_context(db: Session, company_id: int, workbench: dict) -> dict:
    company = db.get(Company, company_id)
    timezone_name = company.timezone if company else None
    email_settings = get_or_create_settings(db, EmailSettings, company_id)
    llm_settings = get_or_create_settings(db, LLMSettings, company_id)
    items = workbench.get("all_items", [])
    scored = [item["score"] for item in items if item.get("score") is not None]
    average_confidence = round(sum(scored) / len(scored)) if scored else 0
    tabs = workbench.get("tab_counts", {})
    summary = workbench.get("summary", {})

    level = "ok"
    label = "Agente activo"
    detail = "Estoy leyendo correos y preparando propuestas para que las revises en un solo sitio."
    if not llm_settings.agent_enabled or llm_settings.provider == "disabled" or llm_settings.agent_mode == "desactivado":
        level = "inactive"
        label = "Agente pausado"
        detail = "El analisis automatico esta detenido desde configuracion."
    elif email_settings.last_sync_ok is False or llm_settings.last_test_ok is False:
        level = "error"
        label = "Agente con error"
        detail = "Necesito que revises la conexion de correo o IA antes de seguir procesando."
    elif not llm_settings.api_key_encrypted:
        level = "warning"
        label = "Agente pendiente de configurar"
        detail = "Falta la API key para usar IA real."

    return {
        "level": level,
        "label": label,
        "detail": detail,
        "last_sync": format_local_datetime(email_settings.last_sync_at, timezone_name, "%H:%M", "Sin lectura registrada"),
        "last_sync_message": "El agente esta atento a nuevas entradas y preparando propuestas.",
        "new_emails": email_settings.last_sync_new or 0,
        "pending_analysis": tabs.get("not_processed", 0),
        "orders_detected": tabs.get("order_detected", 0),
        "ready_to_review": summary.get("ready_to_confirm", 0),
        "needs_attention": tabs.get("attention", 0),
        "errors": tabs.get("errors", 0),
        "average_confidence": average_confidence,
    }


def agent_activity_items(db: Session, company_id: int) -> list[dict]:
    company = db.get(Company, company_id)
    timezone_name = company.timezone if company else None
    logs = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.company_id == company_id,
            or_(
                AuditLog.action.like("email.%"),
                AuditLog.action.like("agent.%"),
                AuditLog.action.like("order.scoring%"),
                AuditLog.action.like("workbench.%"),
            ),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(6)
    ).all()
    templates = {
        "email.fetch_completed": "He revisado el buzon de correo.",
        "email.fetch_error": "No he podido completar la lectura del correo.",
        "email.saved": "He encontrado un correo nuevo para analizar.",
        "email.attachment_saved": "He guardado un adjunto recibido.",
        "email.pdf_text_extracted": "He leido texto de un PDF recibido.",
        "agent.order_created": "He preparado una propuesta de pedido.",
        "agent.no_order_detected": "He marcado un correo como sin pedido.",
        "agent.processing_error": "Necesito ayuda: un correo no se pudo analizar.",
        "order.created": "He generado una nueva propuesta de pedido.",
        "order.updated": "He actualizado la propuesta del pedido.",
        "order.confirmed": "Pedido confirmado por usuario.",
        "order.exported": "He enviado el pedido a gestion.",
        "order.marked_no_order": "He marcado la entrada como no pedido.",
        "order.scoring_calculated": "He recalculado la confianza de una propuesta.",
        "order.line.update": "He ajustado una linea interpretada.",
        "order.line.duplicate": "He duplicado una linea para revisión.",
        "order.line.delete": "He eliminado una linea del pedido.",
        "workbench.email.read": "He leido el correo a peticion del usuario.",
    }
    items = []
    seen_messages: set[str] = set()
    for log in logs:
        message = templates.get(log.action, "He actualizado la cola de trabajo.")
        if message == "He actualizado la cola de trabajo." and items and items[-1]["message"] == message:
            continue
        if message in seen_messages and message == "He actualizado la cola de trabajo.":
            continue
        seen_messages.add(message)
        items.append({"time": format_local_datetime(log.created_at, timezone_name, "%H:%M", "--:--"), "message": message})
        if len(items) >= 5:
            break
    if not items:
        items.append({"time": "--:--", "message": "Estoy listo para leer correos y preparar propuestas."})
    return items


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
        "fields": [
            {"key": "display_name", "label": "Nombre visible"},
            {"key": "phone_number", "label": "Número de línea"},
            {"key": "transcription_provider", "label": "Proveedor transcripción"},
            {"key": "language", "label": "Idioma"},
        ],
        "required_keys": ["phone_number", "transcription_provider"],
    },
    "social": {
        "summary": "Ideal para mensajería social y adjuntos ligeros.",
        "config_title": "Redes sociales",
        "fields": [
            {"key": "display_name", "label": "Nombre visible"},
            {"key": "platform", "label": "Plataforma"},
            {"key": "account_id", "label": "Cuenta / página"},
            {"key": "access_token", "label": "Token de acceso", "secret": True},
            {"key": "webhook_secret", "label": "Secreto webhook", "secret": True},
        ],
        "required_keys": ["platform", "account_id", "access_token"],
    },
}


def channel_status_payload(db: Session, company_id: int, channel: InputChannel) -> dict:
    spec = CHANNEL_CONFIG_SPECS.get(channel.key, {})
    settings_map = channel_settings_map(db, company_id, channel.id)
    email_status = email_config_status(get_or_create_settings(db, EmailSettings, company_id)) if channel.key == "email" else None
    company = db.get(Company, company_id)
    timezone_name = company.timezone if company else None
    last_message = (
        db.scalar(
            select(InboundMessage)
            .where(InboundMessage.company_id == company_id, InboundMessage.channel_id == channel.id)
            .order_by(InboundMessage.last_processed_at.desc().nullslast(), InboundMessage.created_at.desc())
        )
        if channel.key != "email"
        else None
    )
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
                details = "Correo listo para recibir pedidos."
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
            elif last_message and last_message.processing_error:
                state = "error"
                status_label = "Activo · con error"
                details = last_message.processing_error
                activity_label = "Último error"
                activity_value = last_message.processing_error
            else:
                state = "ready"
                status_label = "Activo · configurado"
                details = "Canal operativo con la configuración actual."
                if last_message:
                    stamp = last_message.last_processed_at or last_message.created_at
                    activity_label = "Última actividad"
                    activity_value = f"{format_local_datetime(stamp, timezone_name, '%d/%m %H:%M', 'Sin actividad')} · {last_message.status}"
                else:
                    activity_label = "Última actividad"
                    activity_value = "Sin actividad todavía"
    return {
        "state": state,
        "status_label": status_label,
        "details": details,
        "activity_label": activity_label,
        "activity_value": activity_value,
        "settings": settings_map,
        "email_status": email_status,
        "spec": spec,
    }


def alerts_overview(db: Session, company_id: int, limit: int = 8) -> list[dict]:
    alerts = db.scalars(
        select(Alert)
        .where(Alert.company_id == company_id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    ).all()
    return [serialize_alert(alert) for alert in alerts]


def alert_severity_label(severity: str) -> str:
    return {
        "critical": "Crítica",
        "high": "Alta",
        "medium": "Media",
        "low": "Baja",
        "info": "Informativa",
    }.get(severity, severity.title())


def alert_status_label(status: str) -> str:
    return {
        "open": "Nueva",
        "seen": "Vista",
        "processing": "En proceso",
        "resolved": "Resuelta",
        "ignored": "Ignorada",
    }.get(status, status.title())


def alert_is_active(alert: Alert) -> bool:
    return alert.status not in {"resolved", "ignored"}


def alert_default_action(alert: Alert) -> tuple[str, str]:
    if alert.order_id:
        if alert.alert_type == "export_failed":
            return "Reintentar", f"/orders/{alert.order_id}"
        if alert.alert_type in {"order_review_required", "order_blocked", "automation_blocked"}:
            return "Resolver", f"/orders/{alert.order_id}"
        return "Abrir pedido", f"/orders/{alert.order_id}"
    if alert.inbound_message_id:
        return "Abrir entrada", "/pedidos?kind=emails"
    return "Revisar", "/alerts"


def serialize_alert(alert: Alert) -> dict:
    action_label, action_href = alert_default_action(alert)
    related_entity_type = "pedido" if alert.order_id else "entrada" if alert.inbound_message_id else "sistema"
    related_entity_id = alert.order_id or alert.inbound_message_id or alert.id
    return {
        "id": alert.id,
        "title": alert.title,
        "message": alert.message,
        "severity": alert.severity,
        "severity_label": alert_severity_label(alert.severity),
        "status": alert.status,
        "status_label": alert_status_label(alert.status),
        "type": alert.alert_type,
        "entity_type": related_entity_type,
        "entity_id": related_entity_id,
        "created_at": alert.created_at,
        "created_label": format_local_datetime(alert.created_at, fmt="%d/%m %H:%M", default=""),
        "action_label": action_label,
        "action_href": action_href,
        "secondary_label": "Marcar vista" if alert.status == "open" else "Reabrir" if alert.status in {"resolved", "ignored"} else "Resolver",
        "secondary_action": "mark-read" if alert.status == "open" else "reopen" if alert.status in {"resolved", "ignored"} else "resolve",
        "resolved_at": alert.resolved_at,
        "is_active": alert_is_active(alert),
    }


def build_alert_center_context(db: Session, company_id: int, limit: int = 6) -> dict:
    recent_alerts = db.scalars(
        select(Alert)
        .where(Alert.company_id == company_id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    ).all()
    all_alerts = db.scalars(select(Alert).where(Alert.company_id == company_id)).all()
    active = [alert for alert in all_alerts if alert_is_active(alert)]
    return {
        "total": len(active),
        "critical": len([alert for alert in active if alert.severity == "critical"]),
        "high": len([alert for alert in active if alert.severity == "high"]),
        "medium": len([alert for alert in active if alert.severity == "medium"]),
        "low": len([alert for alert in active if alert.severity == "low"]),
        "info": len([alert for alert in active if alert.severity == "info"]),
        "has_critical": any(alert.severity == "critical" for alert in active),
        "recent": [serialize_alert(alert) for alert in recent_alerts],
    }


def has_admin_access(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


def _learning_status_label(value: str | None) -> str:
    mapping = {
        "pending": "Pendiente",
        "processing": "Procesando",
        "indexed": "Indexado",
        "ready": "Listo",
        "completed": "Procesado",
        "excluded": "Excluido",
        "error": "Error",
        "failed": "Fallido",
    }
    return mapping.get((value or "").lower(), value or "Sin estado")


def _learning_person_label(user: TenantUser | None) -> str:
    return user.name if user else "Sistema"


def _learning_customer_label(customer: Customer | None) -> str:
    if not customer:
        return "Sin cliente"
    return f"{customer.code} · {customer.fiscal_name}"


def _learning_product_label(product: Product | None) -> str:
    if not product:
        return "Sin producto"
    return f"{product.reference} · {product.name}"


def _learning_order_label(order: Order | None) -> str:
    if not order:
        return "Sin pedido"
    return f"Pedido #{order.id}"


def _learning_content_excerpt(text: str | None, limit: int = 220) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def learning_overview(db: Session, company_id: int, limit: int = 12) -> dict:
    email_settings = get_or_create_settings(db, EmailSettings, company_id)
    llm_settings = get_or_create_settings(db, LLMSettings, company_id)
    scoring_settings = get_or_create_settings(db, ScoringSettings, company_id)

    pending_corrections = db.scalar(
        select(func.count()).select_from(ManualCorrection).where(
            ManualCorrection.company_id == company_id,
            ManualCorrection.should_learn == True,  # noqa: E712
        )
    ) or 0
    pending_aliases = db.scalar(
        select(func.count()).select_from(LearnedAlias).where(
            LearnedAlias.company_id == company_id,
            LearnedAlias.approved == False,  # noqa: E712
        )
    ) or 0
    approved_aliases = db.scalar(
        select(func.count()).select_from(LearnedAlias).where(
            LearnedAlias.company_id == company_id,
            LearnedAlias.approved == True,  # noqa: E712
        )
    ) or 0
    learned_corrections = db.scalar(
        select(func.count()).select_from(ManualCorrection).where(
            ManualCorrection.company_id == company_id,
            ManualCorrection.corrected_value.is_not(None),
            ManualCorrection.should_learn == False,  # noqa: E712
        )
    ) or 0
    documents_total = db.scalar(select(func.count()).select_from(RagDocument).where(RagDocument.company_id == company_id)) or 0
    documents_indexed = db.scalar(
        select(func.count()).select_from(RagDocument).where(
            RagDocument.company_id == company_id,
            RagDocument.embedding_status.in_(("indexed", "ready", "completed")),
        )
    ) or 0
    documents_pending = db.scalar(
        select(func.count()).select_from(RagDocument).where(
            RagDocument.company_id == company_id,
            RagDocument.embedding_status.in_(("pending", "processing")),
        )
    ) or 0
    documents_excluded = db.scalar(
        select(func.count()).select_from(RagDocument).where(
            RagDocument.company_id == company_id,
            RagDocument.embedding_status == "excluded",
        )
    ) or 0
    documents_errors = db.scalar(
        select(func.count()).select_from(RagDocument).where(
            RagDocument.company_id == company_id,
            RagDocument.embedding_status.in_(("error", "failed")),
        )
    ) or 0
    import_jobs_total = db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.company_id == company_id)) or 0
    import_jobs_errors = db.scalar(
        select(func.count()).select_from(ImportJob).where(
            ImportJob.company_id == company_id,
            ImportJob.status.in_(("error", "failed")),
        )
    ) or 0
    rag_cases_total = db.scalar(select(func.count()).select_from(RagCase).where(RagCase.company_id == company_id)) or 0
    last_indexed_at = db.scalar(
        select(func.max(RagDocument.created_at)).where(
            RagDocument.company_id == company_id,
            RagDocument.embedding_status.in_(("indexed", "ready", "completed")),
        )
    )
    last_import_at = db.scalar(select(func.max(ImportJob.created_at)).where(ImportJob.company_id == company_id))

    correction_rows = db.scalars(
        select(ManualCorrection)
        .where(ManualCorrection.company_id == company_id)
        .order_by(ManualCorrection.created_at.desc())
        .limit(limit)
    ).all()
    alias_rows = db.scalars(
        select(LearnedAlias)
        .where(LearnedAlias.company_id == company_id)
        .order_by(LearnedAlias.created_at.desc())
        .limit(limit)
    ).all()
    document_rows = db.scalars(
        select(RagDocument)
        .where(RagDocument.company_id == company_id)
        .order_by(RagDocument.created_at.desc())
        .limit(limit)
    ).all()
    case_rows = db.scalars(
        select(RagCase)
        .where(RagCase.company_id == company_id)
        .order_by(RagCase.created_at.desc())
        .limit(limit)
    ).all()
    import_rows = db.scalars(
        select(ImportJob)
        .where(ImportJob.company_id == company_id)
        .order_by(ImportJob.created_at.desc())
        .limit(limit)
    ).all()
    prompt_rows = db.scalars(
        select(PromptTemplate)
        .where(PromptTemplate.company_id == company_id)
        .order_by(PromptTemplate.purpose)
    ).all()

    suggestions: list[dict] = []
    corrections: list[dict] = []
    for correction in correction_rows:
        order = db.get(Order, correction.order_id) if correction.order_id else None
        customer = db.get(Customer, order.customer_id) if order and order.customer_id else None
        user = db.get(User, correction.created_by_user_id) if correction.created_by_user_id else None
        status = "Pendiente" if correction.should_learn else ("Aprendida" if correction.corrected_value else "Ignorada")
        correction_payload = {
            "id": correction.id,
            "created_at": correction.created_at,
            "type_label": correction.entity_type.replace("_", " ").title(),
            "field_name": correction.field_name,
            "before": correction.original_value or "Sin valor",
            "after": correction.corrected_value or "Sin correccion",
            "customer_label": _learning_customer_label(customer),
            "order_label": _learning_order_label(order),
            "user_label": _learning_person_label(user),
            "status": status,
            "status_class": "status-doubtful" if correction.should_learn else "status-confirmed" if correction.corrected_value else "status-discarded",
            "should_learn": correction.should_learn,
            "reason": correction.reason or "",
        }
        corrections.append(correction_payload)
        if correction.should_learn:
            suggestions.append(
                {
                    "kind": "correction",
                    "id": correction.id,
                    "type_label": correction.entity_type.replace("_", " ").title(),
                    "detected": correction.original_value or correction.field_name,
                    "suggested": correction.corrected_value or "Sin correccion",
                    "source": correction.field_name,
                    "confidence": None,
                    "status": "Pendiente",
                    "status_class": "status-doubtful",
                    "action_label": "Aceptar",
                    "accept_href": f"/learning/corrections/{correction.id}/accept",
                    "ignore_href": f"/learning/corrections/{correction.id}/ignore",
                    "created_at": correction.created_at,
                    "customer_label": _learning_customer_label(customer),
                    "order_label": _learning_order_label(order),
                }
            )

    for alias in alias_rows:
        customer = db.get(Customer, alias.customer_id) if alias.customer_id else None
        product = db.get(Product, alias.product_id) if alias.product_id else None
        if not alias.approved:
            suggestions.append(
                {
                    "kind": "alias",
                    "id": alias.id,
                    "type_label": "Alias",
                    "detected": alias.alias,
                    "suggested": alias.canonical_value,
                    "source": alias.alias_type,
                    "confidence": alias.confidence,
                    "status": "Pendiente",
                    "status_class": "status-doubtful",
                    "action_label": "Aprobar",
                    "accept_href": f"/learning/aliases/{alias.id}/approve",
                    "ignore_href": f"/learning/aliases/{alias.id}/ignore",
                    "created_at": alias.created_at,
                    "customer_label": _learning_customer_label(customer),
                    "product_label": _learning_product_label(product),
                }
            )

    suggestions.sort(key=lambda item: item["created_at"], reverse=True)

    document_items: list[dict] = []
    for document in document_rows:
        owner_label = ""
        if document.source_entity == "customer" and document.source_entity_id:
            owner_label = _learning_customer_label(db.get(Customer, document.source_entity_id))
        elif document.source_entity == "product" and document.source_entity_id:
            owner_label = _learning_product_label(db.get(Product, document.source_entity_id))
        document_items.append(
            {
                "id": document.id,
                "title": document.title,
                "source_label": document.source_type.replace("_", " ").title(),
                "entity_label": document.source_entity.replace("_", " ").title(),
                "owner_label": owner_label,
                "status": document.embedding_status,
                "status_label": _learning_status_label(document.embedding_status),
                "created_at": document.created_at,
                "excerpt": _learning_content_excerpt(document.content_text, 260),
                "content_text": document.content_text or "",
                "can_index": document.embedding_status not in {"indexed", "completed"},
                "can_exclude": document.embedding_status != "excluded",
            }
        )

    history_items: list[dict] = []
    for job in import_rows:
        try:
            mapping_used = json.loads(job.mapping_used or "{}")
        except json.JSONDecodeError:
            mapping_used = {}
        history_items.append(
            {
                "id": job.id,
                "entity_type": job.entity_type,
                "filename": job.filename,
                "status": job.status,
                "status_label": _learning_status_label(job.status),
                "rows_total": job.rows_total,
                "rows_created": job.rows_created,
                "rows_updated": job.rows_updated,
                "rows_ignored": job.rows_ignored,
                "created_at": job.created_at,
                "mapping_preview": ", ".join(list(mapping_used.keys())[:3]) if mapping_used else "Sin mapeo",
            }
        )

    case_items: list[dict] = []
    for case in case_rows:
        customer = db.get(Customer, case.customer_id) if case.customer_id else None
        order = db.get(Order, case.order_id) if case.order_id else None
        case_items.append(
            {
                "id": case.id,
                "summary": case.summary,
                "resolved_action": case.resolved_action,
                "score": case.similarity_score,
                "created_at": case.created_at,
                "customer_label": _learning_customer_label(customer),
                "order_label": _learning_order_label(order),
            }
        )

    return {
        "summary": {
            "pending_suggestions": pending_corrections + pending_aliases,
            "pending_corrections": pending_corrections,
            "pending_aliases": pending_aliases,
            "approved_aliases": approved_aliases,
            "learned_corrections": learned_corrections,
            "documents_total": documents_total,
            "documents_indexed": documents_indexed,
            "documents_pending": documents_pending,
            "documents_excluded": documents_excluded,
            "import_jobs_total": import_jobs_total,
            "rag_cases_total": rag_cases_total,
            "learning_errors": documents_errors + import_jobs_errors,
            "last_indexing_at": last_indexed_at,
            "last_import_at": last_import_at,
            "human_review_required": email_settings.always_human_review,
            "agent_mode": llm_settings.agent_mode,
            "safety_level": llm_settings.safety_level,
            "safe_threshold": scoring_settings.safe_threshold,
            "review_threshold": scoring_settings.review_threshold,
            "doubtful_threshold": scoring_settings.doubtful_threshold,
        },
        "suggestions": suggestions,
        "corrections": corrections,
        "documents": document_items,
        "histories": history_items,
        "cases": case_items,
        "prompts": [{"id": row.id, "name": row.name, "purpose": row.purpose, "active": row.active_version_id is not None} for row in prompt_rows],
        "settings": {
            "llm": llm_settings,
            "scoring": scoring_settings,
            "email": email_settings,
        },
    }


def channel_settings_overview(db: Session, company_id: int) -> list[dict]:
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.is_default.desc(), InputChannel.name)).all()
    rows = []
    for channel in channels:
        settings = db.scalars(select(ChannelSetting).where(ChannelSetting.company_id == company_id, ChannelSetting.channel_id == channel.id).order_by(ChannelSetting.key)).all()
        payload = channel_status_payload(db, company_id, channel)
        rows.append({
            "channel": channel,
            "raw_settings": settings,
            "settings_count": len(settings),
            "capabilities": channel_capabilities(channel),
            **payload,
        })
    return rows


def channel_settings_overview_fallback(db: Session, company_id: int) -> list[dict]:
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.is_default.desc(), InputChannel.name)).all()
    return [
        {
            "channel": channel,
            "raw_settings": [],
            "settings_count": 0,
            "capabilities": channel_capabilities(channel),
            "state": "inactive",
            "status_label": "Inactivo",
            "details": "No se pudo leer el detalle de configuración.",
            "activity_label": "Sin actividad",
            "activity_value": "Estado temporalmente no disponible.",
            "settings": {},
            "email_status": email_config_status(get_or_create_settings(db, EmailSettings, company_id)) if channel.key == "email" else None,
            "spec": CHANNEL_CONFIG_SPECS.get(channel.key, {}),
        }
        for channel in channels
    ]
