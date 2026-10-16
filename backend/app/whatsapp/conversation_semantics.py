from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agent.platform import UnifiedOrderPipelineService
from app.db.models import InboundMessage
from app.whatsapp.conversation_orders import has_explicit_order_close


FAQ_NON_DISTINCTIVE_TERMS = {
    "al", "algo", "algunos", "algunas", "con", "cual", "cuales", "como", "cuando",
    "de", "del", "donde", "el", "ella", "ellas", "ellos", "en", "es", "esta",
    "este", "esto", "estos", "hay", "la", "las", "lo", "los", "me", "mi", "mis",
    "para", "por", "que", "se", "su", "sus", "un", "una", "unas", "unos", "y",
    "cliente", "clientes", "empresa", "empresas", "entrega", "entregas", "pedido",
    "pedidos", "producto", "productos", "teneis", "tienen", "tenemos",
}


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
    contextual_order_followup: bool = False


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
    query_terms = _terms(query) - FAQ_NON_DISTINCTIVE_TERMS
    if not query_terms:
        return []
    scored = [
        (len(query_terms & (_terms(entry["question"]) - FAQ_NON_DISTINCTIVE_TERMS)), entry["id"])
        for entry in entries
    ]
    best = max((score for score, _ in scored), default=0)
    minimum_match = 1 if len(query_terms) == 1 else 2
    if best < minimum_match:
        return []
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


def _is_simple_greeting(text: str) -> bool:
    normalized = _normalized(text).strip().strip("¡¿!?. ,")
    return bool(
        re.fullmatch(
            r"(?:hola+|buenas?(?:\s+(?:dias|tardes|noches))?|buenos\s+dias|hey|ey|saludos)",
            normalized,
        )
    )


def _requests_human_handoff(text: str) -> bool:
    normalized = _normalized(text)
    has_human_target = bool(
        re.search(r"\b(?:persona|humano|humana|agente|asesor|asesora|operador|operadora|representante|alguien)\b", normalized)
    )
    if not has_human_target:
        return False
    return bool(
        re.search(
            r"\b(?:quiero|necesito|quisiera|querria|prefiero|puedo|podria|hablar|contactar|"
            r"pasame|pasarme|pasas|ponme|conectame|derivame|atenderme|atiendeme|comunicarme)\b",
            normalized,
        )
    )


def _is_short_binary_reply(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:s[ií]|no|correct[oa]|de acuerdo|adelante|confirmo|confirmad[oa]|vale|ok|okay)[.! ]*\s*",
            _normalized(text),
            flags=re.IGNORECASE,
        )
    )


def _empty_draft() -> dict[str, Any]:
    return {"customer_name": "", "lines": [], "notes": ""}


def _line_identity(line: dict[str, Any]) -> set[str]:
    values = (line.get("reference"), line.get("product_query"), line.get("product_name"))
    return {
        _normalized(value).strip()
        for value in values
        if _normalized(value).strip()
    }


def _explicit_note_correction(text: str) -> bool:
    normalized = _normalized(text).strip()
    if not normalized:
        return False
    note_field = r"(?:entrega|entregar|fecha|direccion|horario|observaciones?|notas?|comentario)"
    explicit_correction = bool(
        re.search(
            r"\b(?:cambia(?:r)?|corrige|corregir|actualiza(?:r)?|sustituye|sustituir|"
            r"reemplaza|reemplazar|en lugar de|en vez de|perdona|perdon|al final|mejor)\b",
            normalized,
        )
        or re.search(rf"\bno\b.{{0,80}}\b{note_field}\b", normalized)
    )
    return explicit_correction and bool(re.search(rf"\b{note_field}\b", normalized))


def _note_categories(value: str) -> set[str]:
    normalized = _normalized(value)
    categories: set[str] = set()
    if re.search(
        r"\b(?:fecha|dia|lunes|martes|miercoles|jueves|viernes|sabado|domingo|manana|tarde|noche|"
        r"hora|horario|plazo|antes|despues)\b|\b\d{1,2}(?::\d{2})?\b",
        normalized,
    ):
        categories.add("schedule")
    if re.search(
        r"\b(?:direccion|domicilio|calle|avenida|avda|portal|puerta|piso|recepcion|muelle|"
        r"codigo postal|localidad|municipio)\b",
        normalized,
    ):
        categories.add("location")
    if re.search(r"\b(?:entrega|entregar)\b", normalized):
        categories.add("delivery")
    return categories or {"other"}


def _note_correction_scope(text: str) -> set[str] | None:
    if not _explicit_note_correction(text):
        return None
    normalized = _normalized(text)
    if re.search(
        r"\b(?:direccion|domicilio|calle|avenida|avda|portal|puerta|piso|recepcion|muelle|"
        r"codigo postal|localidad|municipio)\b",
        normalized,
    ):
        return {"location"}
    if re.search(
        r"\b(?:fecha|dia|lunes|martes|miercoles|jueves|viernes|sabado|domingo|manana|tarde|noche|"
        r"hora|horario|plazo|antes|despues)\b|\b\d{1,2}(?::\d{2})?\b",
        normalized,
    ):
        return {"schedule"}
    if re.search(r"\b(?:entrega|entregar)\b", normalized):
        return {"delivery"}
    return {"all"}


def _explicit_product_replacement(
    previous_lines: list[Any],
    extracted_lines: list[Any],
    latest_message_text: str,
) -> tuple[int, set[str], list[dict[str, Any]]] | None:
    normalized_text = _normalized(latest_message_text).strip()
    if not normalized_text:
        return None

    old_product_patterns = (
        r"\b(?:cambia(?:r)?|sustituye|sustituir|reemplaza|reemplazar|corrige|corregir)\b(?P<old>.*?)\b(?:por|con)\b",
        r"\ben vez de\b(?P<old>.*?)(?:[,;]|\b(?:usa|elige|pon|pide|quiero|mejor)\b)",
        r"\ben lugar de\b(?P<old>.*?)(?:[,;]|\b(?:usa|elige|pon|pide|quiero|mejor)\b)",
        r"\bno\b(?P<old>.*?)\b(?:sino|en su lugar)\b",
    )
    old_product_text = next(
        (
            match.group("old").strip()
            for pattern in old_product_patterns
            if (match := re.search(pattern, normalized_text))
            and match.group("old").strip()
        ),
        "",
    )
    if not old_product_text:
        # Natural corrections often omit the old product ("perdona, me
        # refería a Caja mediana"). Only infer the target when there is one
        # saved line; with multiple lines the intended replacement is unclear.
        if len(previous_lines) == 1 and re.search(
            r"\b(?:perdona|perdon|me referia a|queria decir|al final era|en realidad era)\b",
            normalized_text,
        ):
            extracted = [line for line in extracted_lines if isinstance(line, dict)]
            if len(extracted) == 1 and _line_identity(extracted[0]) != _line_identity(previous_lines[0]):
                return 0, _line_identity(previous_lines[0]), extracted
        return None

    mentioned_previous = []
    for index, line in enumerate(previous_lines):
        if not isinstance(line, dict):
            continue
        identities = _line_identity(line)
        if any(
            re.search(rf"(?<!\w){re.escape(identity)}(?!\w)", old_product_text)
            for identity in identities
        ):
            mentioned_previous.append((index, identities))
    if len(mentioned_previous) != 1:
        return None

    target_index, target_identity = mentioned_previous[0]
    replacements = [
        dict(line)
        for line in extracted_lines
        if isinstance(line, dict)
        and (identity := _line_identity(line))
        and not identity.intersection(target_identity)
    ]
    if not replacements:
        return None
    return target_index, target_identity, replacements


def _merge_order_drafts(
    previous: dict[str, Any],
    extracted: dict[str, Any],
    *,
    latest_message_text: str = "",
) -> tuple[dict[str, Any], bool]:
    """Keep earlier order lines when a later turn only extracts a fragment.

    A matching line is refreshed with the latest extraction (so corrections can
    replace an earlier quantity); unmatched lines from the saved draft survive.
    The boolean indicates that at least one previous line had to be preserved.
    """

    previous_lines = previous.get("lines") if isinstance(previous.get("lines"), list) else []
    extracted_lines = extracted.get("lines") if isinstance(extracted.get("lines"), list) else []
    available_previous = [
        line for line in previous_lines[:30] if isinstance(line, dict)
    ]
    correction = _explicit_product_replacement(
        available_previous,
        extracted_lines[:30],
        latest_message_text,
    )
    if correction:
        target_index, target_identity, replacement_lines = correction
        remaining_previous = [
            line
            for index, line in enumerate(available_previous)
            if index != target_index
        ]
        remaining_previous_identities = [
            _line_identity(line) for line in remaining_previous
        ]
        new_replacement_lines = [
            line
            for line in replacement_lines
            if not any(
                _line_identity(line).intersection(identity)
                for identity in remaining_previous_identities
            )
        ]
        replacement_identities = [_line_identity(line) for line in new_replacement_lines]
        remaining_extracted = [
            line
            for line in extracted_lines[:30]
            if not isinstance(line, dict)
            or not (
                _line_identity(line).intersection(target_identity)
                or any(
                    _line_identity(line).intersection(identity)
                    for identity in replacement_identities
                )
            )
        ]
        merged, preserved = _merge_order_drafts(
            {**previous, "lines": remaining_previous},
            {**extracted, "lines": remaining_extracted},
            latest_message_text=latest_message_text,
        )
        merged_lines = merged["lines"]
        insert_at = min(target_index, len(merged_lines))
        merged_lines[insert_at:insert_at] = new_replacement_lines
        merged["lines"] = merged_lines[:30]
        return merged, preserved

    matched_previous: set[int] = set()
    merged_lines: list[dict[str, Any]] = [dict(line) for line in available_previous]

    for extracted_line in extracted_lines[:30]:
        if not isinstance(extracted_line, dict):
            continue
        extracted_identity = _line_identity(extracted_line)
        match_index = next(
            (
                index
                for index, previous_line in enumerate(available_previous)
                if index not in matched_previous
                and extracted_identity
                and extracted_identity.intersection(_line_identity(previous_line))
            ),
            None,
        )
        if match_index is None:
            merged_lines.append(dict(extracted_line))
            continue

        matched_previous.add(match_index)
        previous_line = merged_lines[match_index]
        merged_line = dict(previous_line)
        for key, value in extracted_line.items():
            if key == "quantity":
                if value is not None:
                    merged_line[key] = value
            elif value not in (None, "", []):
                merged_line[key] = value
        merged_lines[match_index] = merged_line

    unmatched_previous = [
        line for index, line in enumerate(available_previous) if index not in matched_previous
    ]

    previous_notes = str(previous.get("notes") or "").strip()
    extracted_notes = str(extracted.get("notes") or "").strip()
    correction_scope = _note_correction_scope(latest_message_text)
    notes: list[str] = []
    seen_notes: set[str] = set()
    previous_note_parts = [
        note.strip()
        for note in re.split(r"[\n;]+", previous_notes)
        if note.strip()
    ]
    for note in previous_note_parts:
        categories = _note_categories(note)
        if correction_scope and ("all" in correction_scope or categories.intersection(correction_scope)):
            continue
        key = _normalized(note).strip()
        if key not in seen_notes:
            seen_notes.add(key)
            notes.append(note)
    for note in re.split(r"[\n;]+", extracted_notes):
        note = note.strip()
        key = _normalized(note).strip()
        if note and key not in seen_notes:
            seen_notes.add(key)
            notes.append(note)
    merged_notes = "\n".join(notes)[:1000]
    return (
        {
            "customer_name": str(extracted.get("customer_name") or previous.get("customer_name") or ""),
            "lines": merged_lines,
            "notes": merged_notes,
        },
        bool(unmatched_previous),
    )


def _preserved_previous_uncertainties(
    previous_missing: list[str],
    *,
    previous_draft: dict[str, Any],
    extracted_draft: dict[str, Any],
    merged_draft: dict[str, Any],
    latest_message_text: str,
) -> list[str]:
    previous_lines = [
        line for line in (previous_draft.get("lines") or [])[:30] if isinstance(line, dict)
    ]
    extracted_lines = [
        line for line in (extracted_draft.get("lines") or [])[:30] if isinstance(line, dict)
    ]
    matched_previous: set[int] = set()
    for extracted_line in extracted_lines:
        identity = _line_identity(extracted_line)
        if not identity:
            continue
        match = next(
            (
                index
                for index, previous_line in enumerate(previous_lines)
                if index not in matched_previous and identity.intersection(_line_identity(previous_line))
            ),
            None,
        )
        if match is not None:
            matched_previous.add(match)

    correction = _explicit_product_replacement(
        previous_lines,
        extracted_lines,
        latest_message_text,
    )
    if correction:
        matched_previous.add(correction[0])

    preserved_indexes = set(range(len(previous_lines))) - matched_previous
    if not preserved_indexes:
        return []

    merged_lines = [
        line for line in (merged_draft.get("lines") or [])[:30] if isinstance(line, dict)
    ]
    retained: list[str] = []
    for item in previous_missing:
        match = re.search(r"\bl[ií]nea\s+(\d+)\b", _normalized(item))
        if not match:
            retained.append(item)
            continue
        previous_index = int(match.group(1)) - 1
        if previous_index not in preserved_indexes:
            continue
        previous_identity = _line_identity(previous_lines[previous_index])
        merged_index = next(
            (
                index
                for index, line in enumerate(merged_lines)
                if previous_identity.intersection(_line_identity(line))
            ),
            previous_index,
        )
        if merged_index != previous_index:
            item = re.sub(
                r"(?i)(\bl[ií]nea\s+)\d+",
                lambda value: f"{value.group(1)}{merged_index + 1}",
                item,
                count=1,
            )
        retained.append(item)
    return list(dict.fromkeys(retained))


def _is_contextual_order_followup(
    text: str,
    *,
    previous_lines: list[Any],
    previous_missing: list[str],
) -> bool:
    if not previous_lines or not text.strip():
        return False
    normalized = _normalized(text).strip()
    explicit_addition = bool(
        re.search(
            r"\b(?:anade|agrega|suma|incluye|incorpora|apunta|mete|pon(?:me|nos)?|"
            r"quiero\s+tambien|necesito\s+tambien|tambien\s+(?:quiero|necesito))\b",
            normalized,
        )
    )
    explicit_correction = bool(
        re.search(
            r"\b(?:corrige|corregir|cambia|cambiar|sustituye|sustituir|reemplaza|reemplazar|"
            r"perdona|perdon|me referia a|queria decir|al final era|en realidad era)\b",
            normalized,
        )
    )
    explicit_delivery_update = bool(
        re.search(r"\b(?:entrega|entregar|fecha de entrega|direccion|domicilio|observaciones|nota)\b", normalized)
    )
    if not previous_missing and not (explicit_addition or explicit_correction or explicit_delivery_update):
        return False
    if _is_question(text) and not explicit_addition:
        return False
    if _is_simple_greeting(text):
        return False
    if re.search(r"\b(?:reclamaci[oó]n|incidencia|problema|devoluci[oó]n|rotura)\b", normalized):
        return False
    if re.fullmatch(r"(?:gracias|ok|okay|vale|perfecto|si|s[ií]|correcto|de acuerdo)[.! ]*", normalized):
        return False
    if re.search(r"\b(?:nada mas|eso es todo|cierra el pedido|confirma el pedido|envia el pedido)\b", normalized):
        return False

    has_order_detail = bool(
        re.search(r"\d", normalized)
        or re.search(r"\b(?:uds?|unidades?|cajas?|paquetes?|bolsas?|kg|kilos?|litros?|metros?|rollos?|referencia|modelo|codigo|pedido|adjunto)\b", normalized)
    )
    missing_expects_detail = any(
        re.search(
            r"\b(?:producto|referencia|modelo|cantidad|unidad|medida|formato|linea|fecha|entrega|direccion|direcci[oó]n|color|tamano|tama[nñ]o|nombre|empresa|observaciones?)\b",
            _normalized(item),
        )
        for item in previous_missing
    )
    known_product_terms = set().union(
        *(_terms(" ".join(_line_identity(line))) for line in previous_lines if isinstance(line, dict))
    ) if previous_lines else set()
    text_terms = _terms(text)
    return (
        explicit_addition
        or explicit_correction
        or explicit_delivery_update
        or has_order_detail
        or (missing_expects_detail and len(text_terms) >= 1)
        or bool(known_product_terms & text_terms)
    )


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
    if len(raw_lines) > 30:
        uncertainties.append(
            "El pedido supera el límite de 30 líneas y necesita una revisión completa del equipo."
        )
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
    reply_context_text: str | None = None,
    inbound_message: InboundMessage | None = None,
    input_reference: str | None = None,
    reconcile_order_transcript: bool = False,
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
    classification_text = latest_text
    quoted_context = str(reply_context_text or "").strip()[:1500]
    if quoted_context:
        classification_text = (
            f"Mensaje previo relacionado con la respuesta: {quoted_context}\n"
            f"Respuesta actual del cliente: {latest_text}"
        )
    classification = pipeline.classify_inbound_text(
        db,
        company_id,
        classification_text,
        input_reference=input_reference,
    )
    kind = str(
        classification.get("tipo_correo")
        or classification.get("type")
        or classification.get("tipo")
        or ""
    ).strip().lower()
    confidence = _confidence(classification.get("confianza", classification.get("confidence")))
    previous_draft = draft if isinstance(draft, dict) else _empty_draft()
    previous_lines = previous_draft.get("lines") if isinstance(previous_draft.get("lines"), list) else []
    previous_missing = [str(item).strip() for item in (missing_or_uncertain or []) if str(item).strip()]
    reconcile_existing_order = bool(reconcile_order_transcript and previous_lines)
    simple_greeting = (
        kind != "incidencia"
        and _is_simple_greeting(latest_text)
    )
    quoted_binary_order_reply = bool(
        previous_lines
        and previous_missing
        and quoted_context
        and "?" in quoted_context
        and _is_short_binary_reply(latest_text)
    )
    explicit_order_close_followup = bool(
        reconcile_existing_order and has_explicit_order_close(latest_text)
    )
    contextual_order_followup = (
        kind != "incidencia"
        and kind in {"consulta", "dudoso", "no_pedido"}
        and (
            quoted_binary_order_reply
            or explicit_order_close_followup
            or _is_contextual_order_followup(
                latest_text,
                previous_lines=previous_lines,
                previous_missing=previous_missing,
            )
        )
    )
    classification_requires_handoff = (
        _requests_human_handoff(latest_text)
        or kind == "incidencia"
        or (confidence < 0.45 and not contextual_order_followup and not simple_greeting)
    )

    if simple_greeting:
        intent = "greeting"
    elif kind == "pedido" or contextual_order_followup:
        intent = "order"
    elif kind == "consulta":
        if contextual_order_followup:
            intent = "order"
        else:
            intent = "order_question" if previous_lines else "question"
    elif kind == "no_pedido":
        if contextual_order_followup:
            intent = "order"
        elif _is_question(latest_text):
            intent = "order_question" if previous_lines else "question"
        else:
            intent = "other"
    else:
        intent = "other"

    evidence_ids: list[str] = []
    missing: list[str] = []
    result_draft = (
        previous_draft
        if intent in {"question", "order_question", "other", "greeting"}
        else _empty_draft()
    )
    # Do not silently drop live customer turns just because the shared email
    # classifier labels them as non-orders. The agent will use a safe prompt
    # for neutral messages and grounded answers/handoff for questions.
    action = "ask" if intent == "other" else "respond"
    reply_needed = intent in {"order", "order_question", "question", "greeting", "other"}

    if classification_requires_handoff:
        action = "handoff"
        reply_needed = True
        result_draft = previous_draft
        missing.extend(previous_missing)
    elif intent == "order" or reconcile_existing_order:
        extraction_text = transcript or f"CLIENTE: {latest_text}"
        extraction_uncertainties: list[str] = []
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
            if previous_lines:
                result_draft = previous_draft
                missing.extend(previous_missing)
            else:
                missing.append("No he podido identificar las líneas del pedido. Indica productos y cantidades.")
        if extraction:
            extracted_draft, extraction_uncertainties = _map_extraction(
                pipeline,
                extraction,
                customer=customer,
            )
            result_draft, preserved_previous_lines = _merge_order_drafts(
                previous_draft,
                extracted_draft,
                latest_message_text=latest_text,
            )
            missing.extend(extraction_uncertainties)
            if preserved_previous_lines:
                missing.extend(
                    _preserved_previous_uncertainties(
                        previous_missing,
                        previous_draft=previous_draft,
                        extracted_draft=extracted_draft,
                        merged_draft=result_draft,
                        latest_message_text=latest_text,
                    )
                )
        if intent == "order" and not result_draft.get("lines"):
            missing.append("Indicar los productos y cantidades del pedido.")
        action = (
            "handoff"
            if any("supera el límite de 30 líneas" in item.casefold() for item in missing)
            else "ask" if missing else "respond"
        )
        if intent in {"question", "order_question"}:
            faq_entries = _faq_entries(business_faq)
            faq_ids = _faq_evidence_ids(latest_text, faq_entries)
            product_ids = _product_evidence_ids(latest_text, products)
            evidence_ids = faq_ids or product_ids
            action = "respond" if evidence_ids else "handoff"
            if reconcile_existing_order and extraction_uncertainties:
                action = "handoff"
        elif intent != "order":
            action = "handoff" if reconcile_existing_order and extraction_uncertainties else "silent"
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
        contextual_order_followup=contextual_order_followup,
    )
