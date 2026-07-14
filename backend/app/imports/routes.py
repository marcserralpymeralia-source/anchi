import json
from html import unescape
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.services import MatchingService, ScoringService
from app.auth.dependencies import current_user
from app.core.config import get_settings
from app.master.service import TenantUser
from app.core.templating import templates
from app.db.models import Company, ImportJob, ImportMappingTemplate, LLMSettings, Order, OrderLine, PromptTemplate, PromptVersion, ScoringSettings, User
from app.jobs.service import enqueue_job
from app.imports.service import confirm_import, create_preview, read_preview, read_table_from_bytes, validate_import
from app.logs.service import log_action
from app.settings.agent_config import agent_metrics, agent_status
from app.settings.integrations import classify_sample, extract_sample
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/imports", tags=["imports"])


def _active_prompt_content(db: Session, company_id: int, purpose: str) -> str:
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
    if not template or not template.active_version_id:
        defaults = {
            "classification": "Clasifica el texto como pedido, no_pedido, consulta, incidencia o dudoso. Responde solo JSON con tipo_correo, confianza y motivo.",
            "extraction": "Extrae un pedido en JSON valido con cliente y pedido.lineas. Cada linea debe incluir texto_original, referencia_detectada, producto_detectado, cantidad, unidad y confianza_extraccion.",
        }
        return defaults.get(purpose, "Responde en JSON valido.")
    version = db.get(PromptVersion, template.active_version_id)
    return version.content if version else "Responde en JSON valido."


def _score_category(score: float | None, scoring: ScoringSettings) -> tuple[str, str]:
    if score is None:
        return "without_score", "Sin scoring"
    if score >= scoring.safe_threshold:
        return "safe", "Seguro"
    if score >= scoring.review_threshold:
        return "reviewable", "Revisable"
    if score >= scoring.doubtful_threshold:
        return "doubtful", "Dudoso"
    return "not_importable", "No importable"


def _normalize_expected(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _text_from_upload(file: UploadFile | None) -> tuple[str, str]:
    if not file or not file.filename:
        return "", ""
    content = file.file.read()
    try:
        df = read_table_from_bytes(content, file.filename)
    finally:
        try:
            file.file.seek(0)
        except Exception:
            pass
    if list(df.columns) == ["texto"]:
        text = "\n".join(str(value).strip() for value in df["texto"].tolist() if str(value).strip())
    else:
        rows: list[str] = []
        for row in df.head(12).astype(str).itertuples(index=False, name=None):
            rows.append(" | ".join(part.strip() for part in row if part and str(part).strip()))
        text = "\n".join(rows)
    return text.strip(), file.filename


def _parse_line_block(raw_text: str) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for raw_line in (raw_text or "").splitlines():
        text = raw_line.strip()
        if not text:
            continue
        pieces = [piece.strip() for piece in text.replace(";", "|").split("|")]
        while len(pieces) < 4:
            pieces.append("")
        lines.append(
            {
                "original_text": text,
                "reference": pieces[0] or None,
                "product_name": pieces[1] or None,
                "quantity": pieces[2] or None,
                "unit": pieces[3] or None,
                "confidence": 0.8,
            }
        )
    return lines or [{"original_text": raw_text[:180], "reference": None, "product_name": None, "quantity": None, "unit": None, "confidence": 0.5}]


def _analysis_context(
    db: Session,
    user: TenantUser,
    sample_text: str,
    sender: str,
    subject: str,
    expected_customer: str,
    expected_score: str,
    expected_status: str,
    *,
    source_label: str,
) -> dict:
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    matching = MatchingService()
    classification = {"ok": False, "message": "Clasificacion no ejecutada.", "content": ""}
    extraction_payload: dict | None = None
    agent_text = sample_text.strip()
    if not agent_text:
        agent_text = f"Asunto: {subject}\nRemitente: {sender}".strip()
    try:
        classification = classify_sample(llm, _active_prompt_content(db, user.company_id, "classification"), agent_text)
        if classification.get("ok"):
            extraction = extract_sample(llm, _active_prompt_content(db, user.company_id, "extraction"), agent_text)
            if extraction.get("ok"):
                extraction_payload = json.loads(extraction.get("content", "{}"))
    except Exception as exc:  # noqa: BLE001
        classification = {"ok": False, "message": str(exc), "content": ""}

    customer_data = {}
    extracted_lines = []
    if extraction_payload:
        customer_data = extraction_payload.get("cliente") or extraction_payload.get("customer") or {}
        if isinstance(customer_data, str):
            customer_data = {"nombre_detectado": customer_data}
        order_data = extraction_payload.get("pedido") or extraction_payload.get("order") or extraction_payload
        extracted_lines = order_data.get("lineas") or order_data.get("lines") or extraction_payload.get("lineas") or []
        if not isinstance(extracted_lines, list):
            extracted_lines = []

    detected_name = customer_data.get("nombre_detectado") or customer_data.get("name") or customer_data.get("nombre") or expected_customer or None
    detected_code = customer_data.get("codigo_cliente_detectado") or customer_data.get("code") or customer_data.get("codigo")
    customer, customer_method, customer_score = matching.find_customer(
        db,
        user.company_id,
        detected_name=detected_name,
        detected_code=detected_code,
        sender=sender or None,
    )

    lines_source = extracted_lines or _parse_line_block(agent_text)
    preview_lines: list[dict[str, object]] = []
    temp_lines: list[SimpleNamespace] = []
    for raw_line in lines_source:
        reference = raw_line.get("referencia_detectada") or raw_line.get("reference")
        product_name = raw_line.get("producto_detectado") or raw_line.get("product_name") or raw_line.get("descripcion") or raw_line.get("description")
        product, product_method, product_score = matching.find_product(db, user.company_id, reference=reference, detected_name=product_name)
        quantity_value = raw_line.get("cantidad") or raw_line.get("quantity")
        try:
            quantity = float(str(quantity_value).replace(",", ".")) if quantity_value not in {None, ""} else None
        except ValueError:
            quantity = None
        line_confidence = float(raw_line.get("confianza_extraccion") or raw_line.get("confidence") or 0.75)
        preview_lines.append(
            {
                "original_text": raw_line.get("texto_original") or raw_line.get("original_text") or "",
                "reference": reference or "",
                "product_name": product_name or "",
                "matched_product": f"{product.reference} · {product.name}" if product else "Sin coincidencia",
                "match_method": product_method,
                "quantity": quantity,
                "unit": raw_line.get("unidad") or raw_line.get("unit") or "",
                "confidence": round(line_confidence * 100, 1),
                "score": round(product_score * 80 + line_confidence * 20, 1),
                "has_match": bool(product),
            }
        )
        temp_lines.append(SimpleNamespace(product_id=product.id if product else None, quantity=quantity, extraction_confidence=line_confidence))

    temp_order = SimpleNamespace(company_id=user.company_id, customer_id=customer.id if customer else None, lines=temp_lines)
    score = ScoringService().score_order(db, temp_order)
    category, category_label = _score_category(score, scoring)
    expected_score_value = None
    try:
        expected_score_value = float(str(expected_score).replace(",", ".")) if expected_score else None
    except ValueError:
        expected_score_value = None
    result = {
        "classification": classification,
        "customer": {
            "name": customer.fiscal_name if customer else detected_name or "Sin cliente",
            "method": customer_method,
            "score": round(customer_score * 100, 1),
            "matched": bool(customer),
        },
        "lines": preview_lines,
        "score": round(score, 1),
        "category": category,
        "category_label": category_label,
        "status": "pedido_pendiente_revision" if category != "safe" else "pedido_confirmado",
        "source_text": agent_text,
        "source_label": source_label,
        "expected": {
            "customer": expected_customer.strip(),
            "score": expected_score_value,
            "status": expected_status.strip(),
        },
    }
    result["comparison"] = {
        "customer_match": bool(expected_customer and _normalize_expected(expected_customer) in _normalize_expected(result["customer"]["name"])),
        "score_delta": round(score - expected_score_value, 1) if expected_score_value is not None else None,
        "status_match": bool(expected_status and _normalize_expected(expected_status) == _normalize_expected(result["status"])),
    }
    result["suggested_action"] = "Procesar" if category == "safe" else "Revisar" if category in {"reviewable", "doubtful"} else "Bloquear"
    return result


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
            raw = await file.read()
            df = read_table_from_bytes(raw, file.filename)
            file_name = file.filename
            if list(df.columns) == ["texto"]:
                file_text = "\n".join(str(value).strip() for value in df["texto"].tolist() if str(value).strip())
            else:
                file_text = "\n".join(" | ".join(part.strip() for part in row if part and str(part).strip()) for row in df.head(12).astype(str).itertuples(index=False, name=None))
        except Exception as exc:  # noqa: BLE001
            file_text = f"[Error leyendo archivo: {exc}]"
    combined_text = "\n\n".join(piece for piece in [sample_text.strip(), file_text.strip()] if piece)
    if not combined_text.strip():
        combined_text = f"Asunto: {subject}\nRemitente: {sender}".strip()
    source_label = f"Documento: {file_name}" if file_name else "Texto pegado"
    result = _analysis_context(db, user, combined_text, sender, subject, expected_customer, expected_score, expected_status, source_label=source_label)
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
