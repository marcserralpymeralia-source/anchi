from __future__ import annotations

from types import SimpleNamespace

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.services import MatchingService, ScoringService
from app.db.models import LLMSettings, PromptTemplate, PromptVersion, ScoringSettings
from app.imports.service import read_table_from_bytes
from app.settings.integrations import classify_sample, extract_sample
from app.settings.service import get_or_create_settings


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
        except Exception:  # pragma: no cover - defensive rewind
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
        if not text or ("|" not in text and ";" not in text):
            continue

        pieces = [piece.strip() for piece in text.replace(";", "|").split("|")]
        if len([piece for piece in pieces if piece]) < 2:
            continue

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
    return lines


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


def analysis_context(
    db: Session,
    user,
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
        classification = classify_sample(db, llm, user.company_id, agent_text, _active_prompt_content(db, user.company_id, "classification"))
        if classification.get("ok"):
            extraction = extract_sample(db, llm, user.company_id, agent_text, _active_prompt_content(db, user.company_id, "extraction"))
            if extraction.get("ok"):
                extraction_payload = extraction.get("validated_content")
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
