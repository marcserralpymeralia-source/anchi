import hashlib
import json
from datetime import date
from html import unescape

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.templating import templates
from app.db.models import Company, ImportJob, ImportMappingTemplate, InboundMessage, LLMSettings, Order, OrderLine, ScoringSettings
from app.jobs.service import enqueue_job
from app.imports.quick import analysis_context, _text_from_upload
from app.imports.service import confirm_import, create_preview, read_preview, validate_import
from app.logs.service import log_action
from app.messages.service import upsert_inbound_message
from app.settings.agent_config import agent_metrics, agent_status
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db
from app.whatsapp.importer import parse_manual_whatsapp_text
from app.whatsapp.service import get_or_create_whatsapp_channel

router = APIRouter(prefix="/imports", tags=["imports"])


def _normalize_manual_channel(channel: str) -> str:
    if not isinstance(channel, str):
        channel = getattr(channel, "default", "whatsapp")
    normalized = (channel or "").strip().lower()
    if normalized in {"email", "whatsapp"}:
        return normalized
    raise HTTPException(status_code=422, detail="Canal no valido. Usa email o whatsapp.")


def _normalize_manual_text(value: str | object, default: str = "") -> str:
    if not isinstance(value, str):
        value = getattr(value, "default", default)
    return (value or default or "").strip()


def _normalize_import_kind(value: str | object) -> str:
    if not isinstance(value, str):
        value = getattr(value, "default", "")
    return (value or "").strip().lower()


def _queued_response(request: Request, job_id: int, fallback: str = "/imports/history", target: str | None = None):
    if "application/json" in (request.headers.get("accept") or ""):
        return {"ok": True, "job_id": job_id, "status": "queued", "message": "Trabajo encolado correctamente"}
    return RedirectResponse(target or fallback, status_code=303)


@router.post("/preview")
async def preview_import(
    request: Request,
    entity_type: str = Form(...),
    customer_id: int = Form(0),
    import_kind: str = Form(""),
    encoding: str = Form("utf-8"),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    import_kind_value = _normalize_import_kind(import_kind)
    resolved_entity_type = entity_type
    if customer_id and import_kind_value in {"historico_pedidos", "historico_albaranes", "articulos_habituales"}:
        resolved_entity_type = "customer_knowledge_articles"
    try:
        preview = await create_preview(file, resolved_entity_type, encoding=encoding, customer_id=customer_id or None, import_kind=import_kind_value)
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="import.preview.error", entity_type=resolved_entity_type, message=f"Error previsualizando importacion: {exc}")
        return templates.TemplateResponse("imports/error.html", {"request": request, "user": user, "error": f"No se pudo leer el archivo: {exc}"}, status_code=400)
    templates_ = db.scalars(select(ImportMappingTemplate).where(ImportMappingTemplate.company_id == user.company_id, ImportMappingTemplate.entity_type == resolved_entity_type).order_by(ImportMappingTemplate.name)).all()
    return templates.TemplateResponse("imports/preview.html", {"request": request, "user": user, "preview": preview, "templates": templates_, "encoding": encoding})


def _read_whatsapp_upload(file: UploadFile | None) -> tuple[str, str]:
    if not file or not file.filename:
        return "", ""
    content = file.file.read()
    try:
        file.file.seek(0)
    except Exception:  # pragma: no cover - defensive rewind
        pass
    if not content:
        return "", file.filename
    for encoding in ("utf-8", "latin-1"):
        try:
            return content.decode(encoding), file.filename
        except UnicodeDecodeError:
            continue
    return "", file.filename


def _manual_import_context(
    request: Request,
    db: Session,
    user: TenantUser,
    *,
    channel: str,
    raw_text: str,
    sender: str = "",
    subject: str = "",
    phone: str = "",
    client_participant: str = "",
    company_participant: str = "",
    expected_customer: str = "",
    expected_score: str = "",
    expected_status: str = "",
    preview: dict | None = None,
    result: dict | None = None,
    warning: str = "",
    duplicate_message: InboundMessage | None = None,
) -> dict:
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    metrics = agent_metrics(db, user.company_id, scoring)
    channel = _normalize_manual_channel(channel)
    if preview is None:
        if channel == "whatsapp":
            preview = parse_manual_whatsapp_text(raw_text, client_participant=client_participant, company_participant=company_participant)
        else:
            preview = {
                "kind": "email",
                "channel": "email",
                "sender": sender,
                "subject": subject,
                "body": raw_text,
                "messages": [],
                "participants": {
                    "client": sender or "",
                    "company": "",
                },
                "warnings": [],
                "normalized_text": raw_text,
                "dedupe_hash": "",
                "thread_key": "",
            }
    return {
        "request": request,
        "user": user,
        "company": db.get(Company, user.company_id),
        "llm": llm,
        "scoring": scoring,
        "metrics": metrics,
        "agent_status": agent_status(llm, metrics),
        "channel": channel,
        "channel_label": "WhatsApp" if channel == "whatsapp" else "Email",
        "channel_is_whatsapp": channel == "whatsapp",
        "channel_is_email": channel == "email",
        "raw_text": raw_text,
        "sender": sender,
        "subject": subject,
        "phone": phone,
        "client_participant": client_participant or preview.get("participants", {}).get("client", ""),
        "company_participant": company_participant or preview.get("participants", {}).get("company", ""),
        "expected_customer": expected_customer,
        "expected_score": expected_score,
        "expected_status": expected_status,
        "preview": preview,
        "preview_messages": preview.get("messages", []),
        "result": result,
        "warning": warning,
        "duplicate_message": duplicate_message,
    }


@router.get("/quick")
def quick_import_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    context = {
        "request": request,
        "user": user,
        "company": db.get(Company, user.company_id),
        "llm": llm,
        "scoring": scoring,
        "metrics": agent_metrics(db, user.company_id, scoring),
        "agent_status": agent_status(llm, agent_metrics(db, user.company_id, scoring)),
        "sample_text": "",
        "sender": "",
        "subject": "",
        "expected_customer": "",
        "expected_score": "",
        "expected_status": "",
        "result": None,
    }
    return templates.TemplateResponse("imports/quick.html", context)


@router.get("/manual", name="manual_import_page")
@router.get("/whatsapp")
def whatsapp_import_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    default_channel = "whatsapp" if request.url.path.endswith("/whatsapp") else "email"
    context = _manual_import_context(request, db, user, channel=default_channel, raw_text="", sender="", subject="", phone="")
    return templates.TemplateResponse("imports/whatsapp.html", context)


@router.post("/manual/preview")
@router.post("/whatsapp/preview")
async def whatsapp_import_preview(
    request: Request,
    channel: str = Form("whatsapp"),
    raw_text: str = Form(""),
    sender: str = Form(""),
    sender_hint: str = Form(""),
    subject: str = Form(""),
    phone: str = Form(""),
    client_participant: str = Form(""),
    company_participant: str = Form(""),
    expected_customer: str = Form(""),
    expected_score: str = Form(""),
    expected_status: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _normalize_manual_channel(channel)
    sender_value = _normalize_manual_text(sender) or _normalize_manual_text(sender_hint)
    file_text, file_name = _read_whatsapp_upload(file) if channel == "whatsapp" else _text_from_upload(file)
    combined_text = "\n\n".join(piece for piece in [raw_text.strip(), file_text.strip()] if piece)
    warning = ""
    if file_name and file and not file_text and not raw_text.strip():
        warning = f"No se pudo leer el archivo {file_name}."
    preview = parse_manual_whatsapp_text(combined_text, client_participant=client_participant, company_participant=company_participant) if channel == "whatsapp" else {
        "kind": "email",
        "channel": "email",
        "sender": sender_value,
        "subject": subject,
        "body": combined_text,
        "messages": [],
        "participants": {"client": sender_value or "", "company": ""},
        "warnings": [],
        "normalized_text": combined_text,
        "dedupe_hash": "",
        "thread_key": "",
    }
    context = _manual_import_context(
        request,
        db,
        user,
        channel=channel,
        raw_text=combined_text,
        sender=sender_value,
        subject=subject,
        phone=phone,
        client_participant=client_participant,
        company_participant=company_participant,
        expected_customer=expected_customer,
        expected_score=expected_score,
        expected_status=expected_status,
        preview=preview,
        warning=warning,
    )
    return templates.TemplateResponse("imports/whatsapp.html", context)


@router.post("/manual/process")
@router.post("/whatsapp/process")
async def whatsapp_import_process(
    request: Request,
    channel: str = Form("whatsapp"),
    raw_text: str = Form(...),
    sender: str = Form(""),
    sender_hint: str = Form(""),
    subject: str = Form(""),
    phone: str = Form(""),
    client_participant: str = Form(""),
    company_participant: str = Form(""),
    expected_customer: str = Form(""),
    expected_score: str = Form(""),
    expected_status: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _normalize_manual_channel(channel)
    sender_value = _normalize_manual_text(sender) or _normalize_manual_text(sender_hint)
    parsed = parse_manual_whatsapp_text(raw_text, client_participant=client_participant, company_participant=company_participant) if channel == "whatsapp" else {
        "kind": "email",
        "channel": "email",
        "sender": sender_value,
        "subject": subject,
        "body": raw_text,
        "messages": [],
        "participants": {"client": sender_value or "", "company": ""},
        "warnings": [],
        "normalized_text": raw_text,
        "dedupe_hash": "",
        "thread_key": "",
    }
    sender_effective = sender_value or parsed.get("participants", {}).get("client", "")
    result = analysis_context(
        db,
        user,
        raw_text,
        sender_effective,
        subject,
        expected_customer,
        expected_score,
        expected_status,
        source_label="Importación manual de WhatsApp" if channel == "whatsapp" else "Importación manual de correo",
    )
    context = _manual_import_context(
        request,
        db,
        user,
        channel=channel,
        raw_text=raw_text,
        sender=sender_effective,
        subject=subject,
        phone=phone,
        client_participant=client_participant,
        company_participant=company_participant,
        expected_customer=expected_customer,
        expected_score=expected_score,
        expected_status=expected_status,
        preview=parsed,
        result=result,
    )
    return templates.TemplateResponse("imports/whatsapp.html", context)


@router.post("/manual/confirm")
@router.post("/whatsapp/confirm")
def whatsapp_import_confirm(
    request: Request,
    channel: str = Form("whatsapp"),
    raw_text: str = Form(...),
    sender: str = Form(""),
    sender_hint: str = Form(""),
    subject: str = Form(""),
    phone: str = Form(""),
    client_participant: str = Form(""),
    company_participant: str = Form(""),
    expected_customer: str = Form(""),
    expected_score: str = Form(""),
    expected_status: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _normalize_manual_channel(channel)
    sender_value = _normalize_manual_text(sender) or _normalize_manual_text(sender_hint)
    parsed = parse_manual_whatsapp_text(raw_text, client_participant=client_participant, company_participant=company_participant) if channel == "whatsapp" else {
        "kind": "email",
        "channel": "email",
        "sender": sender_value,
        "subject": subject,
        "body": raw_text,
        "messages": [],
        "participants": {"client": sender_value or "", "company": ""},
        "warnings": [],
        "normalized_text": raw_text,
        "dedupe_hash": hashlib.sha256(f"{user.company_id}|{sender_value}|{subject}|{raw_text}".encode("utf-8")).hexdigest(),
        "thread_key": hashlib.sha256(f"thread|{user.company_id}|{sender_value}|{subject}|{raw_text}".encode("utf-8")).hexdigest(),
    }
    channel_model = get_or_create_whatsapp_channel(db, user.company_id) if channel == "whatsapp" else None
    if channel == "email":
        from app.messages.service import ensure_input_channel
        channel_model = ensure_input_channel(db, user.company_id, key="email", name="Email", provider="manual_import")
    dedupe_hash = parsed["dedupe_hash"]
    existing = db.scalar(
        select(InboundMessage).where(
            InboundMessage.company_id == user.company_id,
            InboundMessage.channel_id == channel_model.id,
            InboundMessage.provider == "manual_import",
            InboundMessage.source_external_id == dedupe_hash,
        )
    )
    if existing:
        context = _manual_import_context(
            request,
            db,
            user,
            channel=channel,
            raw_text=raw_text,
            sender=sender_value,
            subject=subject,
            phone=phone,
            client_participant=client_participant,
            company_participant=company_participant,
            expected_customer=expected_customer,
            expected_score=expected_score,
            expected_status=expected_status,
            preview=parsed,
            warning="Esta conversación ya estaba importada. Se reutiliza la existente.",
            duplicate_message=existing,
        )
        return templates.TemplateResponse("imports/whatsapp.html", context)

    message, conversation = upsert_inbound_message(
        db,
        company_id=user.company_id,
        channel_key=channel,
        provider="manual_import",
        external_id=dedupe_hash,
        sender=sender_value or parsed.get("participants", {}).get("client") or None,
        recipients=[company_participant.strip()] if company_participant.strip() else [],
        subject=subject or ("Conversación WhatsApp importada" if channel == "whatsapp" else "Correo importado manualmente"),
        text_content=raw_text,
        external_thread_id=parsed["thread_key"],
        metadata={
            "import_type": "manual_whatsapp" if channel == "whatsapp" else "manual_email",
            "participants": parsed["participants"],
            "messages": parsed["messages"],
            "warnings": parsed["warnings"],
            "dedupe_hash": dedupe_hash,
        },
        content_type="whatsapp_text" if channel == "whatsapp" else "email_text",
        has_attachments=False,
    )
    message.provider = "manual_import"
    message.source_thread_id = parsed["thread_key"]
    message.source_message_id = dedupe_hash
    message.original_content = raw_text
    message.normalized_text = parsed["normalized_text"]
    message.raw_payload_json = json.dumps({"import_type": "manual_whatsapp" if channel == "whatsapp" else "manual_email", "parsed": parsed}, ensure_ascii=False)
    message.status = "received"
    message.processing_step = "received_manual_import"
    message.last_processed_at = None
    conversation.provider = "manual_import"
    conversation.subject = subject or ("Conversación WhatsApp importada" if channel == "whatsapp" else "Correo importado manualmente")
    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="process_inbound_message",
        payload={"inbound_message_id": message.id, "channel": channel, "source": "manual_import"},
        created_by_user_id=user.id,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="imports.manual.confirm", entity_type="inbound_message", entity_id=message.id, message=f"Importacion manual ({channel}) importada: {message.id}")
    if "application/json" in (request.headers.get("accept") or ""):
        return {"ok": True, "message_id": message.id, "conversation_id": conversation.id, "job_id": job.id}
    return RedirectResponse(f"/entries?focus=inbound-{message.id}", status_code=303)


@router.post("/quick")
async def run_quick_import(
    request: Request,
    sample_text: str = Form(""),
    sender: str = Form(""),
    subject: str = Form(""),
    expected_customer: str = Form(""),
    expected_score: str = Form(""),
    expected_status: str = Form(""),
    save_case: str = Form("off"),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    file_text = ""
    file_name = ""
    if file and file.filename:
        try:
            file_text, file_name = _text_from_upload(file)
        except Exception as exc:  # noqa: BLE001
            file_text = f"[Error leyendo archivo: {exc}]"
    combined_text = "\n\n".join(piece for piece in [sample_text.strip(), file_text.strip()] if piece)
    if not combined_text.strip():
        combined_text = f"Asunto: {subject}\nRemitente: {sender}".strip()
    source_label = f"Documento: {file_name}" if file_name else "Texto pegado"
    result = analysis_context(db, user, combined_text, sender, subject, expected_customer, expected_score, expected_status, source_label=source_label)
    if save_case == "on":
        order = Order(
            company_id=user.company_id,
            email_id=None,
            customer_id=None,
            validated_customer_id=None,
            customer_detected_name=result["customer"]["name"],
            customer_identification_method=result["customer"]["method"],
            customer_score=result["customer"]["score"],
            order_date=date.today().isoformat(),
            notes="Importación rápida",
            score=result["score"],
            status=result["status"],
            review_reasons="Importación rápida",
        )
        db.add(order)
        db.flush()
        for line_data in result["lines"]:
            db.add(
                OrderLine(
                    company_id=user.company_id,
                    order_id=order.id,
                    product_id=None,
                    validated_product_id=None,
                    original_text=line_data["original_text"],
                    detected_reference=line_data["reference"],
                    detected_product=line_data["product_name"],
                    quantity=line_data["quantity"],
                    unit=line_data["unit"],
                    extraction_confidence=line_data["confidence"] / 100,
                    line_score=line_data["score"],
                    validation_status="pending",
                    doubt_reason=None,
                )
            )
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="imports.quick.saved", entity_type="order", entity_id=order.id, message=f"Importación rápida guardada: {order.id}")
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    context = {
        "request": request,
        "user": user,
        "company": db.get(Company, user.company_id),
        "llm": llm,
        "scoring": scoring,
        "metrics": agent_metrics(db, user.company_id, scoring),
        "agent_status": agent_status(llm, agent_metrics(db, user.company_id, scoring)),
        "sample_text": sample_text,
        "sender": sender,
        "subject": subject,
        "expected_customer": expected_customer,
        "expected_score": expected_score,
        "expected_status": expected_status,
        "result": result,
    }
    return templates.TemplateResponse("imports/quick.html", context)


@router.post("/validate")
async def validate_preview(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    form = dict(await request.form())
    entity_type = form["entity_type"]
    filename = form["filename"]
    encoding = form.get("encoding", "utf-8")
    customer_id = int(form.get("customer_id") or 0)
    import_kind = form.get("import_kind", "")
    mapping = {key.removeprefix("map__"): value for key, value in form.items() if key.startswith("map__") and value != "__skip__"}
    try:
        df = read_preview(form["token"], filename, encoding=encoding)
        validation = validate_import(db, company_id=user.company_id, entity_type=entity_type, df=df, mapping=mapping, customer_id=customer_id or None, import_kind=import_kind)
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="import.validate.error", entity_type=entity_type, message=f"Error validando importacion: {exc}")
        return templates.TemplateResponse("imports/error.html", {"request": request, "user": user, "error": f"No se pudo validar el archivo: {exc}"}, status_code=400)
    return templates.TemplateResponse(
        "imports/validate.html",
        {
            "request": request,
            "user": user,
            "entity_type": entity_type,
            "filename": filename,
            "token": form["token"],
            "encoding": encoding,
            "customer_id": customer_id or None,
            "import_kind": import_kind,
            "mapping": mapping,
            "mapping_json": json.dumps(mapping),
            "validation": validation,
        },
    )


@router.post("/confirm")
async def confirm_preview(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    form = dict(await request.form())
    entity_type = form["entity_type"]
    filename = form["filename"]
    customer_id = int(form.get("customer_id") or 0)
    import_kind = form.get("import_kind", "")
    mapping = json.loads(unescape(form["mapping_json"]))
    try:
        df = read_preview(form["token"], filename, encoding=form.get("encoding", "utf-8"))
        job = confirm_import(
            db,
            company_id=user.company_id,
            user=user,
            entity_type=entity_type,
            filename=filename,
            df=df,
            mapping=mapping,
            mode=form.get("mode", "create_update"),
            customer_id=customer_id or None,
            import_kind=import_kind,
            save_template=form.get("save_template") == "on",
            template_name=form.get("template_name", ""),
        )
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="import.confirm.error", entity_type=entity_type, message=f"Error confirmando importacion: {exc}")
        return templates.TemplateResponse("imports/error.html", {"request": request, "user": user, "error": f"No se pudo importar: {exc}"}, status_code=400)
    log_action(db, company_id=user.company_id, user=user, action="import.confirm", entity_type="job", entity_id=job.id, message=f"Importacion {entity_type} completada")
    target = "/products" if entity_type == "products" else (f"/customers/{customer_id}/knowledge?section=articles" if entity_type == "customer_knowledge_articles" and customer_id else "/customers")
    if "application/json" in (request.headers.get("accept") or ""):
        return {
            "ok": True,
            "job_id": job.id,
            "status": "completed",
            "message": f"Importacion {entity_type} completada",
            "rows_created": job.rows_created,
            "rows_updated": job.rows_updated,
            "rows_ignored": job.rows_ignored,
        }
    return RedirectResponse(
        f"{target}?imported=1&page=1&page_size=100&created={job.rows_created}&updated={job.rows_updated}&ignored={job.rows_ignored}",
        status_code=303,
    )


@router.get("/history")
def import_history(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.company_id == user.company_id).order_by(ImportJob.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse("imports/history.html", {"request": request, "user": user, "jobs": jobs})
