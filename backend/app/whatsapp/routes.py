from __future__ import annotations

from collections import Counter
import hmac
import json
import logging
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.config import get_settings
from app.master.database import get_master_db
from app.master.service import TenantUser
from app.tenancy.database import tenant_db_session
from app.tenancy.database import get_tenant_db
from app.logs.service import log_action
from app.whatsapp.service import (
    enqueue_whatsapp_media_download,
    enqueue_whatsapp_processing,
    parse_payload_events,
    persist_event,
    resolve_company_from_slug,
    resolve_company_from_whatsapp_identifiers,
    whatsapp_event_matches_config,
    whatsapp_event_has_processable_content,
    whatsapp_event_requires_media_download,
    whatsapp_ingress_is_ready,
    verify_signature,
    verify_webhook_token,
    whatsapp_config,
    send_manual_response,
)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])
logger = logging.getLogger(__name__)


def _webhook_payload_summary(
    payload: dict,
    events: list[dict] | None = None,
    *,
    body_size: int = 0,
    signature_present: bool = False,
) -> dict:
    """Return diagnostics safe to write to logs without storing webhook data."""

    entries = payload.get("entry") if isinstance(payload.get("entry"), list) else []
    changes = [
        change
        for entry in entries
        if isinstance(entry, dict)
        for change in (entry.get("changes") if isinstance(entry.get("changes"), list) else [])
        if isinstance(change, dict)
    ]
    fields = sorted({str(change.get("field") or "unknown").strip().lower() for change in changes})
    kind_counts = Counter(str(event.get("kind") or "unknown") for event in (events or []))
    return {
        "payload_object": str(payload.get("object") or "unknown")[:80],
        "entry_count": len(entries),
        "change_count": len(changes),
        "webhook_fields": fields,
        "event_count": sum(kind_counts.values()),
        "event_kinds": dict(sorted(kind_counts.items())),
        "body_size_bytes": max(int(body_size or 0), 0),
        "signature_present": bool(signature_present),
    }


def _webhook_error_metadata(exc: Exception) -> dict[str, str]:
    """Keep exception diagnostics useful while never copying provider error text."""

    return {"error_type": exc.__class__.__name__}


def _webhook_event_metadata(event: dict) -> dict[str, object]:
    """Return safe per-event diagnostics without copying IDs or message content."""

    event_metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return {
        "event_kind": str(event.get("kind") or "unknown")[:80],
        "webhook_field": str(event_metadata.get("webhook_field") or "unknown")[:80],
        "business_account_id_present": bool(event.get("business_account_id")),
        "phone_number_id_present": bool(event.get("phone_number_id")),
    }


def _log_webhook_activity(
    *,
    request: Request,
    action: str,
    message: str,
    metadata: dict | None = None,
    level: int = logging.INFO,
    db: Session | None = None,
    company_id: int | None = None,
) -> None:
    """Write one platform trace and, when possible, one tenant audit event."""

    details = dict(metadata or {})
    logger.log(
        level,
        message,
        extra={
            "event": action,
            "company_id": company_id,
            "path": request.url.path,
            "method": request.method,
            **details,
        },
    )
    if db is None or company_id is None:
        return
    try:
        log_action(
            db,
            company_id=company_id,
            user=None,
            action=action,
            entity_type="whatsapp_webhook",
            message=message,
            metadata=details,
        )
    except Exception:  # noqa: BLE001
        # Observability must never turn a valid Meta response into a webhook retry.
        logger.exception(
            "whatsapp.audit_log_failed",
            extra={
                "event": "whatsapp.audit_log_failed",
                "company_id": company_id,
                "path": request.url.path,
                "method": request.method,
                "action": action,
            },
        )


def _verification_metadata(*, request: Request, challenge_present: bool, token_present: bool) -> dict:
    return {
        "status": "success",
        "challenge_present": challenge_present,
        "verify_token_present": token_present,
        "path": request.url.path,
    }


def _verify_default_webhook_request(request: Request):
    settings = get_settings()
    challenge = request.query_params.get("hub.challenge")
    verify_token = request.query_params.get("hub.verify_token")
    mode = request.query_params.get("hub.mode")
    base_metadata = {
        "path": request.url.path,
        "mode": mode or "",
        "challenge_present": bool(challenge),
        "verify_token_present": bool(verify_token),
    }
    if mode != "subscribe":
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_verification_rejected",
            message="Verificación WhatsApp rechazada: modo no válido.",
            metadata={**base_metadata, "status": "error", "reason": "invalid_mode"},
            level=logging.WARNING,
        )
        return PlainTextResponse("forbidden", status_code=403)
    if not settings.meta_whatsapp_verify_token or not verify_token:
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_verification_rejected",
            message="Verificación WhatsApp rechazada: token ausente o no configurado.",
            metadata={**base_metadata, "status": "error", "reason": "missing_verify_token"},
            level=logging.WARNING,
        )
        return PlainTextResponse("forbidden", status_code=403)
    if not hmac.compare_digest(settings.meta_whatsapp_verify_token, verify_token):
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_verification_rejected",
            message="Verificación WhatsApp rechazada: token no válido.",
            metadata={**base_metadata, "status": "error", "reason": "invalid_verify_token"},
            level=logging.WARNING,
        )
        return PlainTextResponse("forbidden", status_code=403)
    _log_webhook_activity(
        request=request,
        action="whatsapp.webhook_verified",
        message="Verificación WhatsApp completada correctamente.",
        metadata=_verification_metadata(
            request=request,
            challenge_present=bool(challenge),
            token_present=True,
        ),
    )
    return PlainTextResponse(challenge or "ok")


async def _receive_default_webhook_request(
    request: Request,
    x_hub_signature_256: str | None,
    master_db: Session,
):
    raw_body = await request.body()
    summary = {
        "body_size_bytes": len(raw_body),
        "signature_present": bool(x_hub_signature_256),
        "path": request.url.path,
    }
    _log_webhook_activity(
        request=request,
        action="whatsapp.webhook_started",
        message="Solicitud de webhook WhatsApp recibida.",
        metadata={**summary, "status": "running", "route_scope": "default"},
    )
    if not verify_signature(get_settings().meta_app_secret, raw_body, x_hub_signature_256):
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_invalid_signature",
            message="Webhook WhatsApp rechazado: firma no válida.",
            metadata={**summary, "status": "error", "reason": "invalid_signature"},
            level=logging.WARNING,
        )
        return JSONResponse({"ok": False, "message": "invalid signature"}, status_code=403)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_invalid_json",
            message="Webhook WhatsApp rechazado: JSON no válido.",
            metadata={**summary, "status": "error", "reason": "invalid_json"},
            level=logging.WARNING,
        )
        return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)

    payload = payload if isinstance(payload, dict) else {}
    try:
        events = parse_payload_events(payload)
    except Exception as exc:  # noqa: BLE001
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_failed",
            message="No se pudo interpretar el webhook WhatsApp.",
            metadata={
                **summary,
                "status": "error",
                "route_scope": "default",
                **_webhook_error_metadata(exc),
            },
            level=logging.ERROR,
        )
        raise
    if not events:
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_no_events",
            message="Webhook WhatsApp recibido sin eventos procesables.",
            metadata={
                **_webhook_payload_summary(
                    payload,
                    events,
                    body_size=len(raw_body),
                    signature_present=True,
                ),
                "status": "info",
                "reason": "no_events",
                "route_scope": "default",
            },
        )
        return {"ok": True, "events": 0, "stored": [], "ignored": 0}
    stored: list[int] = []
    handled = 0
    queued_processing = 0
    queued_media = 0
    not_processable = 0
    ignored_reasons: Counter[str] = Counter()
    for event in events:
        try:
            company, tenant_db = resolve_company_from_whatsapp_identifiers(
                master_db,
                business_account_id=event.get("business_account_id"),
                phone_number_id=event.get("phone_number_id"),
            )
        except Exception as exc:  # noqa: BLE001
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_failed",
                message="No se pudo resolver el tenant del evento WhatsApp.",
                metadata={
                    "status": "error",
                    **_webhook_event_metadata(event),
                    "route_scope": "default",
                    **_webhook_error_metadata(exc),
                },
                level=logging.ERROR,
            )
            raise
        if not company or not tenant_db:
            ignored_reasons["tenant_not_found_or_identifiers_mismatch"] += 1
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_event_ignored",
                message="Evento WhatsApp ignorado porque no se pudo resolver el tenant.",
                metadata={
                    "status": "info",
                    "reason": "tenant_not_found_or_identifiers_mismatch",
                    **_webhook_event_metadata(event),
                    "route_scope": "default",
                },
                level=logging.WARNING,
            )
            continue
        config_db = tenant_db_session(tenant_db.database_url)()
        try:
            config = whatsapp_config(config_db, company.id)
            if event.get("kind") != "account_update" and not whatsapp_ingress_is_ready(config_db, company.id, config=config):
                ignored_reasons["tenant_not_ready"] += 1
                _log_webhook_activity(
                    request=request,
                    action="whatsapp.webhook_event_ignored",
                    message="Evento WhatsApp ignorado porque el canal no está listo.",
                    metadata={
                        "status": "info",
                        "reason": "tenant_not_ready",
                        **_webhook_event_metadata(event),
                        "route_scope": "default",
                    },
                    level=logging.WARNING,
                    db=config_db,
                    company_id=company.id,
                )
                continue
            if not whatsapp_event_matches_config(event, config):
                ignored_reasons["event_identifiers_mismatch"] += 1
                _log_webhook_activity(
                    request=request,
                    action="whatsapp.webhook_event_ignored",
                    message="Evento WhatsApp ignorado porque no coincide con la configuración del tenant.",
                    metadata={
                        "status": "info",
                        "reason": "event_identifiers_mismatch",
                        **_webhook_event_metadata(event),
                        "route_scope": "default",
                    },
                    level=logging.WARNING,
                    db=config_db,
                    company_id=company.id,
                )
                continue
            handled += 1
            message = persist_event(config_db, company.id, event)
            queue_action = "duplicate_or_not_persisted"
            if message:
                stored.append(message.id)
                if event.get("kind") == "message":
                    if whatsapp_event_requires_media_download(event):
                        enqueue_whatsapp_media_download(config_db, company.id, message.id)
                        queued_media += 1
                        queue_action = "download_whatsapp_media"
                    elif whatsapp_event_has_processable_content(event):
                        enqueue_whatsapp_processing(config_db, company.id, message.id)
                        queued_processing += 1
                        queue_action = "process_inbound_message"
                    else:
                        not_processable += 1
                        queue_action = "not_processable"
                        message.processing_step = "received_without_processable_content"
                        message.processing_error = "El mensaje no contiene texto procesable ni un adjunto compatible."
            config_db.commit()
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_event_accepted",
                message="Evento WhatsApp aceptado para el tenant.",
                metadata={
                    "status": "success",
                    "company_id": company.id,
                    **_webhook_event_metadata(event),
                    "persisted": bool(message),
                    "queue_action": queue_action,
                    "route_scope": "default",
                },
                db=config_db,
                company_id=company.id,
            )
        except Exception as exc:  # noqa: BLE001
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_failed",
                message="Error procesando un evento WhatsApp.",
                metadata={
                    "status": "error",
                    **_webhook_event_metadata(event),
                    "route_scope": "default",
                    **_webhook_error_metadata(exc),
                },
                level=logging.ERROR,
                db=config_db,
                company_id=company.id,
            )
            raise
        finally:
            config_db.close()
    result_metadata = {
        **_webhook_payload_summary(
            payload,
            events,
            body_size=len(raw_body),
            signature_present=True,
        ),
        "status": "success",
        "handled_count": handled,
        "stored_count": len(stored),
        "queued_processing_count": queued_processing,
        "queued_media_count": queued_media,
        "not_processable_count": not_processable,
        "ignored_count": sum(ignored_reasons.values()),
        "ignored_reasons": dict(sorted(ignored_reasons.items())),
        "route_scope": "default",
    }
    _log_webhook_activity(
        request=request,
        action="whatsapp.webhook_received",
        message="Webhook WhatsApp recibido y procesado.",
        metadata=result_metadata,
    )
    if ignored_reasons:
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_ignored",
            message="Algunos eventos WhatsApp fueron ignorados.",
            metadata={**result_metadata, "status": "info"},
            level=logging.WARNING,
        )
    return {"ok": True, "events": len(events), "stored": stored, "ignored": sum(ignored_reasons.values())}


@router.get("")
def verify_default_webhook(request: Request):
    return _verify_default_webhook_request(request)


@router.get("/")
def verify_default_webhook_with_trailing_slash(request: Request):
    return _verify_default_webhook_request(request)


@router.post("")
async def receive_default_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    return await _receive_default_webhook_request(request, x_hub_signature_256, master_db)


@router.post("/")
async def receive_default_webhook_with_trailing_slash(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    return await _receive_default_webhook_request(request, x_hub_signature_256, master_db)


def _verify_tenant_webhook_request(company_slug: str, request: Request, master_db: Session):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_verification_rejected",
            message="Verificación WhatsApp rechazada: tenant no encontrado.",
            metadata={"status": "error", "reason": "tenant_not_found", "route_scope": "tenant"},
            level=logging.WARNING,
        )
        return PlainTextResponse("unknown tenant", status_code=404)
    config_db = tenant_db_session(tenant_db.database_url)()
    try:
        config = whatsapp_config(config_db, company.id)
        challenge = request.query_params.get("hub.challenge")
        verify_token = request.query_params.get("hub.verify_token") or request.query_params.get("verify_token")
        mode = request.query_params.get("hub.mode") or request.query_params.get("mode")
        base_metadata = {
            "company_id": company.id,
            "mode": mode or "",
            "challenge_present": bool(challenge),
            "verify_token_present": bool(verify_token),
            "route_scope": "tenant",
        }
        if mode and mode not in {"subscribe", "whatsapp"}:
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_verification_rejected",
                message="Verificación WhatsApp rechazada: modo no válido.",
                metadata={**base_metadata, "status": "error", "reason": "invalid_mode"},
                level=logging.WARNING,
                db=config_db,
                company_id=company.id,
            )
            return PlainTextResponse("forbidden", status_code=403)
        if not verify_webhook_token(config, verify_token):
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_verification_rejected",
                message="Verificación WhatsApp rechazada: token no válido.",
                metadata={**base_metadata, "status": "error", "reason": "invalid_verify_token"},
                level=logging.WARNING,
                db=config_db,
                company_id=company.id,
            )
            return PlainTextResponse("forbidden", status_code=403)
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_verified",
            message="Verificación WhatsApp del tenant completada correctamente.",
            metadata={**base_metadata, "status": "success"},
            db=config_db,
            company_id=company.id,
        )
        return PlainTextResponse(challenge or "ok")
    finally:
        config_db.close()


@router.get("/{company_slug}")
def verify_webhook(company_slug: str, request: Request, master_db: Session = Depends(get_master_db)):
    return _verify_tenant_webhook_request(company_slug, request, master_db)


@router.get("/{company_slug}/")
def verify_webhook_with_trailing_slash(company_slug: str, request: Request, master_db: Session = Depends(get_master_db)):
    return _verify_tenant_webhook_request(company_slug, request, master_db)


async def _receive_tenant_webhook_request(
    company_slug: str,
    request: Request,
    x_hub_signature_256: str | None,
    master_db: Session,
):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_tenant_not_found",
            message="Webhook WhatsApp rechazado: tenant no encontrado.",
            metadata={"status": "error", "reason": "tenant_not_found", "route_scope": "tenant"},
            level=logging.WARNING,
        )
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    raw_body = await request.body()
    config_db = tenant_db_session(tenant_db.database_url)()
    try:
        base_metadata = {
            "company_id": company.id,
            "body_size_bytes": len(raw_body),
            "signature_present": bool(x_hub_signature_256),
            "route_scope": "tenant",
        }
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_started",
            message="Solicitud de webhook WhatsApp recibida.",
            metadata={**base_metadata, "status": "running"},
        )
        config = whatsapp_config(config_db, company.id)
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_started",
            message="Solicitud de webhook WhatsApp recibida.",
            metadata={**base_metadata, "status": "running"},
            db=config_db,
            company_id=company.id,
        )
        if not verify_signature(get_settings().meta_app_secret, raw_body, x_hub_signature_256):
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_invalid_signature",
                message="Webhook WhatsApp rechazado: firma no válida.",
                metadata={**base_metadata, "status": "error", "reason": "invalid_signature"},
                level=logging.WARNING,
                db=config_db,
                company_id=company.id,
            )
            return JSONResponse({"ok": False, "message": "invalid signature"}, status_code=403)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_invalid_json",
                message="Webhook WhatsApp rechazado: JSON no válido.",
                metadata={**base_metadata, "status": "error", "reason": "invalid_json"},
                level=logging.WARNING,
                db=config_db,
                company_id=company.id,
            )
            return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
        payload = payload if isinstance(payload, dict) else {}
        events = parse_payload_events(payload)
        if not events:
            metadata = {
                **_webhook_payload_summary(payload, events, body_size=len(raw_body), signature_present=True),
                **base_metadata,
                "status": "info",
                "reason": "no_events",
            }
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_no_events",
                message="Webhook WhatsApp recibido sin eventos procesables.",
                metadata=metadata,
                db=config_db,
                company_id=company.id,
            )
            return {"ok": True, "message": "no events"}
        stored: list[int] = []
        handled = 0
        queued_processing = 0
        queued_media = 0
        not_processable = 0
        ignored_reasons: Counter[str] = Counter()
        ingress_ready = whatsapp_ingress_is_ready(config_db, company.id, config=config)
        for event in events:
            event_metadata = _webhook_event_metadata(event)
            if event.get("kind") != "account_update" and not ingress_ready:
                ignored_reasons["tenant_not_ready"] += 1
                _log_webhook_activity(
                    request=request,
                    action="whatsapp.webhook_event_ignored",
                    message="Evento WhatsApp ignorado porque el canal no está listo.",
                    metadata={
                        "status": "info",
                        "reason": "tenant_not_ready",
                        **event_metadata,
                        "route_scope": "tenant",
                    },
                    level=logging.WARNING,
                    db=config_db,
                    company_id=company.id,
                )
                continue
            if not whatsapp_event_matches_config(event, config):
                ignored_reasons["event_identifiers_mismatch"] += 1
                _log_webhook_activity(
                    request=request,
                    action="whatsapp.webhook_event_ignored",
                    message="Evento WhatsApp ignorado porque no coincide con la configuración del tenant.",
                    metadata={
                        "status": "info",
                        "reason": "event_identifiers_mismatch",
                        **event_metadata,
                        "route_scope": "tenant",
                    },
                    level=logging.WARNING,
                    db=config_db,
                    company_id=company.id,
                )
                continue
            handled += 1
            message = persist_event(config_db, company.id, event)
            queue_action = "duplicate_or_not_persisted"
            if message:
                stored.append(message.id)
                if event.get("kind") == "message":
                    if whatsapp_event_requires_media_download(event):
                        enqueue_whatsapp_media_download(config_db, company.id, message.id)
                        queued_media += 1
                        queue_action = "download_whatsapp_media"
                    elif whatsapp_event_has_processable_content(event):
                        enqueue_whatsapp_processing(config_db, company.id, message.id)
                        queued_processing += 1
                        queue_action = "process_inbound_message"
                    else:
                        not_processable += 1
                        queue_action = "not_processable"
                        message.processing_step = "received_without_processable_content"
                        message.processing_error = "El mensaje no contiene texto procesable ni un adjunto compatible."
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_event_accepted",
                message="Evento WhatsApp aceptado para el tenant.",
                metadata={
                    "status": "success",
                    **event_metadata,
                    "persisted": bool(message),
                    "queue_action": queue_action,
                    "route_scope": "tenant",
                },
                db=config_db,
                company_id=company.id,
            )
        config_db.commit()
        result_metadata = {
            **_webhook_payload_summary(
                payload,
                events,
                body_size=len(raw_body),
                signature_present=True,
            ),
            **base_metadata,
            "status": "success",
            "handled_count": handled,
            "stored_count": len(stored),
            "queued_processing_count": queued_processing,
            "queued_media_count": queued_media,
            "not_processable_count": not_processable,
            "ignored_count": sum(ignored_reasons.values()),
            "ignored_reasons": dict(sorted(ignored_reasons.items())),
        }
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_received",
            message="Webhook WhatsApp recibido y procesado.",
            metadata=result_metadata,
            db=config_db,
            company_id=company.id,
        )
        if ignored_reasons:
            _log_webhook_activity(
                request=request,
                action="whatsapp.webhook_ignored",
                message="Algunos eventos WhatsApp fueron ignorados.",
                metadata={**result_metadata, "status": "info"},
                level=logging.WARNING,
                db=config_db,
                company_id=company.id,
            )
        return {
            "ok": True,
            "company_id": company.id,
            "events": len(events),
            "stored": [item for item in stored if item is not None],
            "ignored": sum(ignored_reasons.values()),
        }
    except Exception as exc:  # noqa: BLE001
        _log_webhook_activity(
            request=request,
            action="whatsapp.webhook_failed",
            message="Error procesando el webhook WhatsApp.",
            metadata={
                "company_id": company.id,
                "body_size_bytes": len(raw_body),
                "signature_present": bool(x_hub_signature_256),
                "status": "error",
                **_webhook_error_metadata(exc),
            },
            level=logging.ERROR,
            db=config_db,
            company_id=company.id,
        )
        raise
    finally:
        config_db.close()


@router.post("/{company_slug}")
async def receive_webhook(
    company_slug: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    return await _receive_tenant_webhook_request(company_slug, request, x_hub_signature_256, master_db)


@router.post("/{company_slug}/")
async def receive_webhook_with_trailing_slash(
    company_slug: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    return await _receive_tenant_webhook_request(company_slug, request, x_hub_signature_256, master_db)


@router.post("/{company_slug}/respond")
async def manual_response(
    company_slug: str,
    conversation_id: int,
    body: str = "",
    template_name: str | None = None,
    template_language: str | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
    master_db: Session = Depends(get_master_db),
):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db or company.id != user.company_id:
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    try:
        message = await send_manual_response(db, company_id=company.id, conversation_id=conversation_id, body=body, user_id=user.id, idempotency_key=idempotency_key, template_name=template_name, template_language=template_language)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    except Exception as exc:  # noqa: BLE001
        error_type = getattr(exc, "error_type", "whatsapp_send_failed")
        status_code = 400 if error_type in {"invalid_message", "recipient_not_found", "response_window_expired", "server_not_configured"} else 409 if error_type in {"send_in_progress", "send_unknown"} else 502
        return JSONResponse({"ok": False, "message": str(exc), "error_type": error_type}, status_code=status_code)
    return {"ok": True, "message_id": message.id, "status": message.status}
