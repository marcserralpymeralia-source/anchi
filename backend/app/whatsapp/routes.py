from __future__ import annotations

import json
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.master.database import get_master_db
from app.master.service import TenantUser
from app.tenancy.database import tenant_db_session
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import (
    enqueue_whatsapp_processing,
    parse_payload_events,
    persist_event,
    resolve_company_from_slug,
    redact_whatsapp_config,
    record_manual_response,
    verify_signature,
    verify_webhook_token,
    whatsapp_config,
)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])


@router.get("/{company_slug}")
def verify_webhook(company_slug: str, request: Request, master_db: Session = Depends(get_master_db)):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        return PlainTextResponse("unknown tenant", status_code=404)
    config_db = tenant_db_session(tenant_db.database_url)()
    try:
        config = whatsapp_config(config_db, company.id)
    finally:
        config_db.close()
    challenge = request.query_params.get("hub.challenge")
    verify_token = request.query_params.get("hub.verify_token") or request.query_params.get("verify_token")
    mode = request.query_params.get("hub.mode") or request.query_params.get("mode")
    if mode and mode not in {"subscribe", "whatsapp"}:
        return PlainTextResponse("forbidden", status_code=403)
    if not verify_webhook_token(config, verify_token):
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge or "ok")


@router.post("/{company_slug}")
async def receive_webhook(
    company_slug: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    raw_body = await request.body()
    config_db = tenant_db_session(tenant_db.database_url)()
    try:
        config = whatsapp_config(config_db, company.id)
        if not verify_signature(config.app_secret, raw_body, x_hub_signature_256):
            return JSONResponse({"ok": False, "message": "invalid signature"}, status_code=403)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
        events = parse_payload_events(payload if isinstance(payload, dict) else {})
        if not events:
            return JSONResponse({"ok": True, "message": "no events"})
        stored = []
        for event in events:
            message = persist_event(config_db, company.id, event)
            stored.append(message.id if message else None)
            if message and event.get("kind") == "message":
                enqueue_whatsapp_processing(config_db, company.id, message.id)
        config_db.commit()
        return {"ok": True, "company_id": company.id, "events": len(events), "stored": [item for item in stored if item is not None]}
    finally:
        config_db.close()


@router.get("/{company_slug}/config")
def whatsapp_config_view(company_slug: str, master_db: Session = Depends(get_master_db)):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    config_db = tenant_db_session(tenant_db.database_url)()
    try:
        config = whatsapp_config(config_db, company.id)
        return {"ok": True, "company": {"id": company.id, "slug": company.slug}, "config": redact_whatsapp_config(config)}
    finally:
        config_db.close()


@router.post("/{company_slug}/respond")
def manual_response(
    company_slug: str,
    conversation_id: int,
    body: str,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
    master_db: Session = Depends(get_master_db),
):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db or company.id != user.company_id:
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    message = record_manual_response(db, company_id=company.id, conversation_id=conversation_id, body=body, user_id=user.id)
    return {"ok": True, "message_id": message.id, "status": message.status}
