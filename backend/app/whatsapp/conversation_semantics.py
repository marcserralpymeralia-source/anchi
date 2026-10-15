from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agent.platform import UnifiedOrderPipelineService
from app.db.models import InboundMessage


@dataclass(slots=True)
class WhatsAppAgentTurnResult:
    intent: str
    action: str
    draft: dict[str, Any]
    missing_or_uncertain: list[str]
    reply_needed: bool
    suggested_reply: str
    confidence: float
    evidence_ids: list[str]
    close_requested: bool
    prompt_execution_id: int | None = None


def _confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1:
        score /= 100
    return min(1.0, max(0.0, score))


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[\w-]{3,}", _normalized(value)))


def _faq_entries(value: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {
            "id": str(item.get("id") or "").strip(),
            "question": str(item.get("question") or "").strip(),
            "answer": str(item.get("answer") or "").strip(),
        }
        for item in parsed[:8]
        if isinstance(item, dict)
        and str(item.get("id") or "").strip()
        and str(item.get("answer") or "").strip()
    ]


def _faq_evidence_ids(query: str, entries: list[dict[str, str]]) -> list[str]:
    query_terms = _terms(query)
    if not query_terms:
        return []
    scored = [
        (len(query_terms & _terms(entry["question"])), entry["id"])
        for entry in entries
    ]
    best = max((score for score, _ in scored), default=0)
    winners = [identifier for score, identifier in scored if score == best and score > 0]
    return winners if len(winners) == 1 else []


def _product_evidence_ids(query: str, products: list[dict[str, Any]]) -> list[str]:
    normalized_query = _normalized(query)
    matches = []
    for product in products:
        identifier = str(product.get("id") or "").strip()
        names = (
            product.get("name"),
            product.get("reference"),
            product.get("alternative_code"),
        )
        if identifier and any(
            (name := _normalized(value).strip()) and name in normalized_query
            for value in names
        ):
            matches.append(identifier)
    return matches


def _is_question(text: str) -> bool:
    normalized = _normalized(text)
    return "?" in normalized or bool(
        re.search(
            r"\b(?:que|quien|cuando|donde|como|cuanto|cual|puedo|podemos|teneis|tienen|hay|cuesta|precio|horario|envio)\b",
            normalized,
        )
    )


def _empty_draft() -> dict[str, Any]:
    return {"customer_name": "", "lines": [], "notes": ""}


def _map_extraction(
    pipeline: UnifiedOrderPipelineService,
    extraction: dict[str, Any],
    *,
    customer: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    customer_data = extraction.get("cliente") or extraction.get("customer") or {}
    if isinstance(customer_data, str):
        customer_data = {"nombre_detectado": customer_data}
    order = extraction.get("pedido") or extraction.get("order") or extraction
    raw_lines = pipeline._lines_from_extraction(extraction)
    lines: list[dict[str, Any]] = []
    uncertainties: list[str] = []
    for index, raw_line in enumerate(raw_lines[:30], start=1):
        if not isinstance(raw_line, dict):
            continue
        lines.append(
            {
                "product_query": str(
                    raw_line.get("producto_detectado")
                    or raw_line.get("product_name")
                    or raw_line.get("product")
                    or raw_line.get("texto_original")
                    or ""
                ).strip(),
                "reference": str(
                    raw_line.get("referencia_detectada")
                    or raw_line.get("reference")
                    or ""
                ).strip(),
                "quantity": raw_line.get("cantidad", raw_line.get("quantity")),
                "unit": str(raw_line.get("unidad") or raw_line.get("unit") or "").strip(),
                "notes": str(raw_line.get("observaciones") or raw_line.get("notes") or "").strip(),
            }
        )
        reasons = raw_line.get("uncertainties") or []
        if isinstance(reasons, list):
            uncertainties.extend(
                str(item.get("reason") or item.get("mensaje") or "").strip()
                if isinstance(item, dict)
                else str(item).strip()
                for item in reasons
            )
        if raw_line.get("requires_review") and not reasons:
            uncertainties.append(f"Confirmar los datos de la línea {index} del pedido.")

    overall = extraction.get("motivos_revision") or extraction.get("review_reasons") or []
    if isinstance(overall, list):
        uncertainties.extend(str(item).strip() for item in overall)

    # WhatsApp identity is established from the tenant-scoped phone match, not
    # from a customer name inferred by the extractor.
    verified_name = str((customer or {}).get("name") or "").strip()
    extracted_name = str(
        customer_data.get("nombre_detectado")
        or customer_data.get("name")
        or customer_data.get("nombre")
        or ""
    ).strip() if isinstance(customer_data, dict) else ""
    draft = {
        "customer_name": verified_name or extracted_name,
        "lines": lines,
        "notes": str(order.get("observaciones") or order.get("notes") or "").strip(),
    }
    return draft, list(dict.fromkeys(item for item in uncertainties if item))


def evaluate_whatsapp_agent_turn(
    db: Session,
    *,
    company_id: int,
    transcript: str,
    draft: dict[str, Any],
    customer: dict[str, Any] | None,
    products: list[dict[str, Any]],
    business_faq: str,
    missing_or_uncertain: list[str] | None = None,
    latest_message_text: str = "",
    inbound_message: InboundMessage | None = None,
    input_reference: str | None = None,
) -> WhatsAppAgentTurnResult:
    """Adapt WhatsApp turns to the classifier and extractor used by Email."""

    transcript = str(transcript or "").strip()
    latest_text = str(latest_message_text or "").strip()
    if not latest_text and transcript:
        customer_turns = re.findall(r"(?:^|\n)CLIENTE:\s*(.+)", transcript, flags=re.I)
        latest_text = customer_turns[-1].strip() if customer_turns else transcript
    if not latest_text:
        raise ValueError("La conversación no contiene texto evaluable.")

    pipeline = UnifiedOrderPipelineService()
    classification = pipeline.classify_inbound_text(
        db,
        company_id,
        latest_text,
        input_reference=input_reference,
    )
    kind = str(
        classification.get("tipo_correo")
        or classification.get("type")
        or classification.get("tipo")
        or ""
    ).strip().lower()
    confidence = _confidence(classification.get("confianza", classification.get("confidence")))
    classification_requires_handoff = kind in {"incidencia", "dudoso"} or confidence < 0.45
    previous_draft = draft if isinstance(draft, dict) else _empty_draft()
    previous_lines = previous_draft.get("lines") if isinstance(previous_draft.get("lines"), list) else []
    previous_missing = [str(item).strip() for item in (missing_or_uncertain or []) if str(item).strip()]

    if kind == "pedido":
        intent = "order"
    elif kind == "consulta":
        if previous_lines and previous_missing and not _is_question(latest_text):
            intent = "order"
        else:
            intent = "order_question" if previous_lines else "question"
    elif kind == "no_pedido":
        if previous_lines and previous_missing and not _is_question(latest_text):
            # Short answers such as "cajas" or "5 kg" continue an order
            # conversation, even though the email classifier calls them
            # non-orders when viewed as isolated messages.
            intent = "order"
        elif previous_lines and _is_question(latest_text):
            intent = "order_question"
        else:
            intent = "other"
    else:
        intent = "other"

    evidence_ids: list[str] = []
    missing: list[str] = []
    result_draft = previous_draft if intent in {"question", "order_question", "other"} else _empty_draft()
    action = "silent" if intent == "other" else "respond"
    reply_needed = intent in {"order", "order_question", "question"}

    if classification_requires_handoff:
        action = "handoff"
        reply_needed = True
    elif intent == "order":
        extraction_text = transcript or f"CLIENTE: {latest_text}"
        try:
            extraction = pipeline.extract_inbound_order(
                db,
                company_id,
                extraction_text,
                inbound_message,
                input_reference=input_reference,
            )
        except ValueError as exc:
            if "no se ha podido crear el pedido" not in str(exc).casefold():
                raise
            extraction = {}
            missing.append("No he podido identificar las líneas del pedido. Indica productos y cantidades.")
        if extraction:
            result_draft, extraction_uncertainties = _map_extraction(
                pipeline,
                extraction,
                customer=customer,
            )
            missing.extend(extraction_uncertainties)
        if not result_draft.get("lines"):
            missing.append("Indicar los productos y cantidades del pedido.")
        action = "ask" if missing else "respond"
    elif intent in {"question", "order_question"}:
        faq_entries = _faq_entries(business_faq)
        faq_ids = _faq_evidence_ids(latest_text, faq_entries)
        product_ids = _product_evidence_ids(latest_text, products)
        evidence_ids = faq_ids or product_ids
        action = "respond" if evidence_ids else "handoff"
    return WhatsAppAgentTurnResult(
        intent=intent,
        action=action,
        draft=result_draft,
        missing_or_uncertain=list(dict.fromkeys(missing)),
        reply_needed=reply_needed,
        # Responses are generated by deterministic, tenant-grounded WhatsApp
        # rules in agent.py, never by a second WhatsApp-specific LLM prompt.
        suggested_reply="",
        confidence=confidence,
        evidence_ids=evidence_ids,
        close_requested=False,
        prompt_execution_id=None,
    )
