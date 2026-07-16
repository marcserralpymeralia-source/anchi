import json
from datetime import date
from html import unescape
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.templating import templates
from app.db.models import Company, ImportJob, ImportMappingTemplate, LLMSettings, Order, OrderLine, ScoringSettings
from app.jobs.service import enqueue_job
from app.imports.quick import analysis_context, _text_from_upload
from app.imports.service import confirm_import, create_preview, read_preview, validate_import
from app.logs.service import log_action
from app.settings.agent_config import agent_metrics, agent_status
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/imports", tags=["imports"])


def _queued_response(request: Request, job_id: int, fallback: str = "/imports/history"):
    if "application/json" in (request.headers.get("accept") or ""):
        return {"ok": True, "job_id": job_id, "status": "queued", "message": "Trabajo encolado correctamente"}
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


@router.post("/preview")
async def preview_import(
    request: Request,
    entity_type: str = Form(...),
    encoding: str = Form("utf-8"),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    try:
        preview = await create_preview(file, entity_type, encoding=encoding)
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="import.preview.error", entity_type=entity_type, message=f"Error previsualizando importacion: {exc}")
        return templates.TemplateResponse("imports/error.html", {"request": request, "user": user, "error": f"No se pudo leer el archivo: {exc}"}, status_code=400)
    templates_ = db.scalars(select(ImportMappingTemplate).where(ImportMappingTemplate.company_id == user.company_id, ImportMappingTemplate.entity_type == entity_type).order_by(ImportMappingTemplate.name)).all()
    return templates.TemplateResponse("imports/preview.html", {"request": request, "user": user, "preview": preview, "templates": templates_, "encoding": encoding})


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
    mapping = {key.removeprefix("map__"): value for key, value in form.items() if key.startswith("map__") and value != "__skip__"}
    try:
        df = read_preview(form["token"], filename, encoding=encoding)
        validation = validate_import(db, company_id=user.company_id, entity_type=entity_type, df=df, mapping=mapping)
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
    mapping = json.loads(unescape(form["mapping_json"]))
    try:
        job = enqueue_job(
            db,
            company_id=user.company_id,
            job_type="import_confirm",
            payload={
                "token": form["token"],
                "filename": filename,
                "entity_type": entity_type,
                "encoding": form.get("encoding", "utf-8"),
                "mapping": mapping,
                "mode": form.get("mode", "create_update"),
                "save_template": form.get("save_template") == "on",
                "template_name": form.get("template_name", ""),
            },
            created_by_user_id=user.id,
        )
    except Exception as exc:
        log_action(db, company_id=user.company_id, user=user, action="import.confirm.error", entity_type=entity_type, message=f"Error confirmando importacion: {exc}")
        return templates.TemplateResponse("imports/error.html", {"request": request, "user": user, "error": f"No se pudo importar: {exc}"}, status_code=400)
    log_action(db, company_id=user.company_id, user=user, action="import.confirm", entity_type="job", entity_id=job.id, message=f"Importacion {entity_type} encolada")
    return _queued_response(request, job.id)


@router.get("/history")
def import_history(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.company_id == user.company_id).order_by(ImportJob.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse("imports/history.html", {"request": request, "user": user, "jobs": jobs})

from app.core.templating import templates
