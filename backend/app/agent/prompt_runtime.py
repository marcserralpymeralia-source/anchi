from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, LEGACY_OPENAI_MODEL_FALLBACK, completion_token_parameter_name, model_supports_reasoning, resolve_openai_runtime_model, resolve_reasoning_effort
from app.core.observability import redact_sensitive_data
from app.db.models import PromptExecution, PromptExecutionDetail, PromptTemplate, PromptVersion
from app.logs.service import log_flow_event


PromptProvider = Callable[[Any, list[dict], str], dict]


PROMPT_REGISTRY: dict[str, dict[str, Any]] = {
    "classification": {
        "name": "Clasificacion de entrada",
        "purpose": "classification",
        "default_model": "gpt-4.1-mini",
        "default_parameters": {"temperature": 0.1, "max_tokens": 1200},
        "expected_schema": {
            "type": "object",
            "required": ["tipo_correo", "confianza", "motivo"],
        },
        "fallback": "Clasifica la entrada como pedido, no_pedido, consulta, incidencia o dudoso. Responde solo JSON con tipo_correo, confianza y motivo.",
        "input_limit": 12000,
    },
    "whatsapp_conversation": {
        "name": "Estado conversacional de WhatsApp",
        "purpose": "whatsapp_conversation",
        "default_model": "gpt-4.1-mini",
        "default_parameters": {"temperature": 0.0, "max_tokens": 900},
        "expected_schema": {
            "type": "object",
            "required": [
                "intent",
                "state",
                "missing_or_uncertain",
                "reply_needed",
                "suggested_reply",
                "confidence",
            ],
        },
        "fallback": (
            "Analiza una conversacion de WhatsApp B2B entre CLIENTE y EMPRESA. "
            "Determina si el cliente esta realizando un pedido y si hay informacion "
            "suficiente para pedir su confirmacion. No inventes productos, cantidades, "
            "unidades ni clientes. Responde solo JSON con: "
            "intent (order, question u other), "
            "state (collecting, needs_clarification o ready_for_confirmation), "
            "missing_or_uncertain (lista de textos), "
            "reply_needed (boolean), suggested_reply (texto) y confidence (0 a 1). "
            "Usa ready_for_confirmation solo cuando el pedido expresado sea suficientemente "
            "claro para resumirlo y pedir confirmacion al cliente. "
            "Si falta o es ambigua informacion necesaria del pedido usa needs_clarification. "
            "Si el cliente todavia parece estar añadiendo contenido usa collecting."
        ),
        "input_limit": 12000,
    },
    "extraction": {
        "name": "Extraccion de pedido",
        "purpose": "extraction",
        "default_model": LEGACY_OPENAI_MODEL_FALLBACK,
        "default_parameters": {"temperature": 0.1, "max_tokens": 2400},
        "expected_schema": {
            "type": "object",
            "required": ["pedido"],
        },
        "fallback": "Extrae un pedido en JSON valido con cliente y pedido.lineas. Cada linea debe incluir texto_original, referencia_detectada, producto_detectado, cantidad, unidad y confianza_extraccion.",
        "input_limit": 16000,
    },
    "validation": {
        "name": "Validacion de pedido",
        "purpose": "validation",
        "default_model": LEGACY_OPENAI_MODEL_FALLBACK,
        "default_parameters": {"temperature": 0.1, "max_tokens": 1600},
        "expected_schema": {
            "type": "object",
            "required": ["advertencias", "bloqueos"],
        },
        "fallback": (
            "Valida la propuesta de pedido frente al texto recibido. "
            "Responde solo JSON con advertencias (lista de textos), "
            "bloqueos (lista de textos), scoring_recomendado (numero) y resumen."
        ),
        "input_limit": 20000,
    },
}

ALLOWED_CLASSIFICATION_TYPES = {"pedido", "no_pedido", "consulta", "incidencia", "dudoso"}
TRACE_PAYLOAD_MAX_CHARS = 250_000
_TRACE_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", flags=re.I)
_TRACE_PHONE_RE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\d[\s.-]?){8,14}(?!\w)")


@dataclass(slots=True)
class PromptDefinition:
    template: PromptTemplate | None
    version: PromptVersion | None
    name: str
    purpose: str
    content: str
    default_model: str
    default_parameters: dict[str, Any]
    expected_schema: dict[str, Any]
    input_limit: int


@dataclass(slots=True)
class PromptValidationResult:
    status: str
    data: dict[str, Any] | None
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def resolve_prompt_definition(db: Session, company_id: int, purpose: str) -> PromptDefinition:
    spec = PROMPT_REGISTRY.get(purpose, {})
    fallback = str(spec.get("fallback") or "")
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
    version = db.get(PromptVersion, template.active_version_id) if template and template.active_version_id else None
    content = version.content if version and version.content else fallback
    return PromptDefinition(
        template=template,
        version=version,
        name=(template.name if template else str(spec.get("name") or purpose.title())),
        purpose=purpose,
        content=content,
        default_model=str(spec.get("default_model") or LEGACY_OPENAI_MODEL_FALLBACK),
        default_parameters=dict(spec.get("default_parameters") or {}),
        expected_schema=dict(spec.get("expected_schema") or {}),
        input_limit=int(spec.get("input_limit") or 12000),
    )


def prompt_registry_snapshot(db: Session, company_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(PromptTemplate).where(PromptTemplate.company_id == company_id).order_by(PromptTemplate.purpose)).all()
    snapshot: list[dict[str, Any]] = []
    for template in rows:
        version = db.get(PromptVersion, template.active_version_id) if template.active_version_id else None
        spec = PROMPT_REGISTRY.get(template.purpose, {})
        snapshot.append(
            {
                "template_id": template.id,
                "name": template.name,
                "purpose": template.purpose,
                "version": version.version if version else 0,
                "model": spec.get("default_model", LEGACY_OPENAI_MODEL_FALLBACK),
                "parameters": dict(spec.get("default_parameters") or {}),
                "expected_schema": dict(spec.get("expected_schema") or {}),
            }
        )
    return snapshot


def _extract_json_content(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise ValueError("Respuesta vacia del proveedor IA.")
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {"value": parsed}
    raise ValueError("OpenAI ha devuelto una respuesta no valida: no es JSON.")


def validate_prompt_output(purpose: str, content: str) -> PromptValidationResult:
    try:
        data = _extract_json_content(content)
    except json.JSONDecodeError:
        return PromptValidationResult(status="invalid_json", data=None, errors=["La respuesta no es JSON valido."])
    except ValueError as exc:
        return PromptValidationResult(status="invalid_json", data=None, errors=[str(exc)])
    if purpose == "classification":
        tipo = str(data.get("tipo_correo") or data.get("type") or data.get("tipo") or "").strip().lower()
        confidence = data.get("confianza", data.get("confidence"))
        if not tipo:
            return PromptValidationResult(status="missing_fields", data=data, errors=["Falta tipo_correo."])
        if tipo not in ALLOWED_CLASSIFICATION_TYPES:
            return PromptValidationResult(status="unsupported_value", data=data, errors=[f"Tipo de correo no soportado: {tipo}."])
        if confidence is None:
            return PromptValidationResult(status="missing_fields", data=data, errors=["Falta confianza."])
        return PromptValidationResult(status="valid", data=data, errors=[])
    if purpose == "whatsapp_conversation":
        intent = str(data.get("intent") or "").strip().lower()
        state = str(data.get("state") or "").strip().lower()
        missing = data.get("missing_or_uncertain")
        reply_needed = data.get("reply_needed")
        suggested_reply = data.get("suggested_reply")
        confidence = data.get("confidence")

        if intent not in {"order", "question", "other"}:
            return PromptValidationResult(
                status="unsupported_value",
                data=data,
                errors=[f"Intent no soportado: {intent or 'vacio'}."],
            )
        if state not in {
            "collecting",
            "needs_clarification",
            "ready_for_confirmation",
        }:
            return PromptValidationResult(
                status="unsupported_value",
                data=data,
                errors=[f"Estado conversacional no soportado: {state or 'vacio'}."],
            )
        if not isinstance(missing, list) or not all(
            isinstance(item, str) for item in missing
        ):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["missing_or_uncertain debe ser una lista de textos."],
            )
        if not isinstance(reply_needed, bool):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["reply_needed debe ser boolean."],
            )
        if not isinstance(suggested_reply, str):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["suggested_reply debe ser texto."],
            )
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["confidence debe ser numerico."],
            )
        if not 0 <= float(confidence) <= 1:
            return PromptValidationResult(
                status="unsupported_value",
                data=data,
                errors=["confidence debe estar entre 0 y 1."],
            )
        if state in {"needs_clarification", "ready_for_confirmation"}:
            if not reply_needed or not suggested_reply.strip():
                return PromptValidationResult(
                    status="schema_error",
                    data=data,
                    errors=[
                        "El estado requiere una respuesta sugerida para el cliente."
                    ],
                )

        return PromptValidationResult(status="valid", data=data, errors=[])

    if purpose == "extraction":
        order = data.get("pedido") or data.get("order") or {}
        if not isinstance(order, dict):
            return PromptValidationResult(status="schema_error", data=data, errors=["La extraccion no contiene pedido en formato objeto."])
        lines = order.get("lineas") or order.get("lines") or data.get("lineas") or []
        if not isinstance(lines, list) or not lines:
            return PromptValidationResult(status="missing_fields", data=data, errors=["La extraccion no contiene lineas de pedido."])
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                return PromptValidationResult(status="schema_error", data=data, errors=[f"La linea {index} no es un objeto valido."])
        return PromptValidationResult(status="valid", data=data, errors=[])
    if purpose == "validation":
        warnings = data.get("advertencias", data.get("warnings", []))
        blocks = data.get("bloqueos", data.get("blocks", []))
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            return PromptValidationResult(status="schema_error", data=data, errors=["La validacion debe contener advertencias como lista de textos."])
        if not isinstance(blocks, list) or not all(isinstance(item, str) for item in blocks):
            return PromptValidationResult(status="schema_error", data=data, errors=["La validacion debe contener bloqueos como lista de textos."])
        return PromptValidationResult(status="valid", data=data, errors=[])
    return PromptValidationResult(status="valid", data=data, errors=[])


def _safe_excerpt(text: str | None, limit: int = 1200) -> str | None:
    if not text:
        return None
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _anonymize_trace_text(text: str) -> str:
    anonymized = _TRACE_EMAIL_RE.sub("[email]", text)
    return _TRACE_PHONE_RE.sub("[telefono]", anonymized)


def _trace_text(value: Any, *, anonymize: bool, limit: int = TRACE_PAYLOAD_MAX_CHARS) -> str | None:
    if value is None:
        return None
    safe = str(redact_sensitive_data(value))
    if anonymize:
        safe = _anonymize_trace_text(safe)
    if len(safe) <= limit:
        return safe
    return safe[: limit - 30].rstrip() + "… [traza truncada]"


def _trace_json(value: Any, *, anonymize: bool) -> str:
    safe = redact_sensitive_data(value)
    serialized = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    return _trace_text(serialized, anonymize=anonymize) or "{}"


def _effective_parameters(settings: Any, model: str) -> dict[str, Any]:
    parameters: dict[str, Any] = {"model": model}
    if not model_supports_reasoning(model):
        parameters["temperature"] = getattr(settings, "temperature", None)
    parameters[completion_token_parameter_name(model)] = getattr(settings, "max_tokens", None)
    reasoning_effort = resolve_reasoning_effort(getattr(settings, "reasoning_effort", None), model)
    if reasoning_effort:
        parameters["reasoning_effort"] = reasoning_effort
    return parameters


def _decision_summary(purpose: str, data: dict[str, Any] | None) -> str | None:
    if not isinstance(data, dict):
        return None
    if purpose == "classification":
        return str(data.get("motivo") or data.get("reason") or "") or None
    if purpose == "validation":
        return str(data.get("resumen") or data.get("summary") or "") or None
    if purpose == "extraction":
        return str(data.get("resumen") or data.get("observaciones") or "") or None
    if purpose == "whatsapp_conversation":
        missing = data.get("missing_or_uncertain") or []
        if isinstance(missing, list) and missing:
            return "Faltan o son inciertos: " + "; ".join(str(item) for item in missing)
        return str(data.get("summary") or "") or None
    return str(data.get("summary") or data.get("motivo") or "") or None


def record_external_prompt_execution(
    db: Session,
    company_id: int,
    purpose: str,
    settings: Any,
    *,
    prompt_name: str,
    model: str,
    system_prompt: str,
    user_input: str,
    assistant_output: str | None = None,
    output_status: str = "valid",
    validation_errors: list[str] | None = None,
    input_reference: str | None = None,
    duration_ms: int = 0,
    provider_metadata: dict[str, Any] | None = None,
    reasoning_summary: str | None = None,
    decision_summary: str | None = None,
) -> int:
    """Persist a trace for provider calls that use a specialised client.

    Structured extraction has its own schema and transport code, so it cannot
    use :func:`run_prompt_execution` directly. This adapter keeps it in the
    same diagnostics model without ever persisting hidden chain-of-thought or
    raw provider responses.
    """
    detailed_logging = bool(getattr(settings, "detailed_llm_logs", False))
    store_payloads = bool(getattr(settings, "store_llm_payloads", False))
    anonymize_logs = bool(getattr(settings, "anonymize_llm_logs", True))
    effective_parameters = _effective_parameters(settings, model)
    safe_metadata = dict(provider_metadata or {})
    usage = safe_metadata.get("usage") if isinstance(safe_metadata.get("usage"), dict) else {}
    errors = [str(error) for error in (validation_errors or []) if str(error).strip()]
    output_text = assistant_output or ""
    execution = PromptExecution(
        company_id=company_id,
        prompt_template_id=None,
        prompt_name=prompt_name,
        prompt_purpose=purpose,
        prompt_version=0,
        model=model,
        parameters_json=json.dumps(effective_parameters, ensure_ascii=False, sort_keys=True),
        input_reference=input_reference,
        output_status=output_status,
        validation_errors_json=json.dumps(errors, ensure_ascii=False) if errors else None,
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        estimated_cost=usage.get("estimated_cost"),
        response_hash=sha256(output_text.encode("utf-8")).hexdigest() if output_text else None,
        response_excerpt=_trace_text(output_text, anonymize=anonymize_logs, limit=1200),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_ms=max(0, int(duration_ms)),
    )
    db.add(execution)
    if detailed_logging or store_payloads:
        db.flush()
        db.add(
            PromptExecutionDetail(
                prompt_execution_id=execution.id,
                company_id=company_id,
                system_prompt_text=_trace_text(system_prompt, anonymize=anonymize_logs) if store_payloads else None,
                user_input_text=_trace_text(user_input, anonymize=anonymize_logs) if store_payloads else None,
                assistant_output_text=_trace_text(output_text, anonymize=anonymize_logs) if store_payloads else None,
                reasoning_summary=_trace_text(reasoning_summary, anonymize=anonymize_logs),
                decision_summary=_trace_text(decision_summary, anonymize=anonymize_logs),
                effective_parameters_json=_trace_json(effective_parameters, anonymize=False),
                provider_metadata_json=_trace_json(safe_metadata, anonymize=anonymize_logs),
                is_anonymized=anonymize_logs,
            )
        )
    db.commit()
    event_name = "ai.failed" if output_status in {"provider_error", "error"} else "ai.completed"
    log_flow_event(
        db,
        company_id=company_id,
        event=event_name,
        stage="ai",
        message=f"Ejecución IA {'fallida' if event_name == 'ai.failed' else 'finalizada'}: {purpose}.",
        entity_type="prompt_execution",
        entity_id=execution.id,
        status="error" if event_name == "ai.failed" else "success",
        metadata={
            "purpose": purpose,
            "prompt_name": prompt_name,
            "model": model,
            "output_status": output_status,
            "input_length": len(user_input or ""),
            "output_length": len(output_text),
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "duration_ms": execution.duration_ms,
            "input_reference": input_reference,
            "prompt_execution_id": execution.id,
            **({"effective_parameters": effective_parameters, "decision_summary": decision_summary} if detailed_logging or store_payloads else {}),
        },
    )
    return execution.id


def run_prompt_execution(
    db: Session,
    company_id: int,
    purpose: str,
    settings,
    text: str,
    *,
    provider_call: PromptProvider,
    input_reference: str | None = None,
    user_id: int | None = None,
    prompt_override: str | None = None,
    prompt_name_override: str | None = None,
) -> dict[str, Any]:
    definition = resolve_prompt_definition(db, company_id, purpose)
    prompt_text = prompt_override or definition.content
    prompt_name = prompt_name_override or definition.name
    model_field = {
        "classification": "classification_model",
        "extraction": "extraction_model",
        "validation": "validation_model",
    }.get(purpose, "extraction_model")
    configured_model = getattr(settings, model_field, None)
    if purpose in {"extraction", "validation"}:
        model = resolve_openai_runtime_model(configured_model, fallback=definition.default_model)
    else:
        model = configured_model or definition.default_model
    effective_parameters = _effective_parameters(settings, model)
    detailed_logging = bool(getattr(settings, "detailed_llm_logs", False))
    store_payloads = bool(getattr(settings, "store_llm_payloads", False))
    anonymize_logs = bool(getattr(settings, "anonymize_llm_logs", True))
    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": text[: definition.input_limit]},
    ]
    input_was_truncated = len(text or "") > len(messages[1]["content"])
    started_at = datetime.now(timezone.utc)
    start = perf_counter()
    log_flow_event(
        db,
        company_id=company_id,
        event="ai.started",
        stage="ai",
        message=f"Ejecución IA iniciada: {purpose}.",
        status="started",
        metadata={
            "purpose": purpose,
            "prompt_name": prompt_name,
            "prompt_version": definition.version.version if definition.version else 0,
            "model": model,
            "input_length": len(text or ""),
            "input_was_truncated": input_was_truncated,
            "input_reference": input_reference,
            **({"effective_parameters": effective_parameters} if detailed_logging or store_payloads else {}),
        },
    )
    try:
        response = provider_call(settings, messages, model)
    except Exception as exc:
        failure_duration_ms = int((perf_counter() - start) * 1000)
        failure_execution = PromptExecution(
            company_id=company_id,
            prompt_template_id=definition.template.id if definition.template else None,
            prompt_name=prompt_name,
            prompt_purpose=purpose,
            prompt_version=definition.version.version if definition.version else 0,
            model=model,
            parameters_json=json.dumps(effective_parameters, ensure_ascii=False, sort_keys=True),
            input_reference=input_reference,
            output_status="provider_error",
            validation_errors_json=json.dumps(["El proveedor IA no completó la ejecución."], ensure_ascii=False),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_ms=failure_duration_ms,
        )
        db.add(failure_execution)
        db.flush()
        if detailed_logging or store_payloads:
            db.add(
                PromptExecutionDetail(
                    prompt_execution_id=failure_execution.id,
                    company_id=company_id,
                    system_prompt_text=_trace_text(prompt_text, anonymize=anonymize_logs) if store_payloads else None,
                    user_input_text=_trace_text(messages[1]["content"], anonymize=anonymize_logs) if store_payloads else None,
                    assistant_output_text=None,
                    reasoning_summary=None,
                    decision_summary=None,
                    effective_parameters_json=_trace_json(effective_parameters, anonymize=False),
                    provider_metadata_json=_trace_json(
                        {
                            "ok": False,
                            "error_type": exc.__class__.__name__,
                            "provider_message": str(exc),
                            "input_was_truncated": input_was_truncated,
                        },
                        anonymize=anonymize_logs,
                    ),
                    is_anonymized=anonymize_logs,
                )
            )
        db.commit()
        log_flow_event(
            db,
            company_id=company_id,
            event="ai.failed",
            stage="ai",
            message=f"Ejecución IA fallida: {purpose}.",
            status="error",
            metadata={
                "purpose": purpose,
                "prompt_name": prompt_name,
                "prompt_version": definition.version.version if definition.version else 0,
                "model": model,
                "input_length": len(text or ""),
                "input_was_truncated": input_was_truncated,
                "input_reference": input_reference,
                "error_type": exc.__class__.__name__,
                "duration_ms": failure_duration_ms,
                "prompt_execution_id": failure_execution.id,
                **({"effective_parameters": effective_parameters} if detailed_logging or store_payloads else {}),
            },
        )
        raise
    duration_ms = int((perf_counter() - start) * 1000)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    response_content = response.get("content") or ""
    validation = validate_prompt_output(purpose, response_content) if response.get("ok") else PromptValidationResult(status="provider_error", data=None, errors=[response.get("message") or "Error llamando al proveedor IA."])
    finished_at = datetime.now(timezone.utc)
    response_excerpt = _trace_text(response_content, anonymize=anonymize_logs, limit=1200)
    response_hash = sha256(response_content.encode("utf-8")).hexdigest() if response_content else None
    decision_summary = _decision_summary(purpose, validation.data if validation.ok else None)
    reasoning_summary = response.get("reasoning_summary") if isinstance(response.get("reasoning_summary"), str) else None
    execution = PromptExecution(
        company_id=company_id,
        prompt_template_id=definition.template.id if definition.template else None,
        prompt_name=prompt_name,
        prompt_purpose=purpose,
        prompt_version=definition.version.version if definition.version else 0,
        model=model,
        parameters_json=json.dumps(effective_parameters, ensure_ascii=False, sort_keys=True),
        input_reference=input_reference,
        output_status=validation.status,
        validation_errors_json=json.dumps(validation.errors, ensure_ascii=False) if validation.errors else None,
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        estimated_cost=usage.get("estimated_cost"),
        response_hash=response_hash,
        response_excerpt=response_excerpt,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
    )
    db.add(execution)
    if detailed_logging or store_payloads:
        db.flush()
        db.add(
            PromptExecutionDetail(
                prompt_execution_id=execution.id,
                company_id=company_id,
                system_prompt_text=_trace_text(prompt_text, anonymize=anonymize_logs) if store_payloads else None,
                user_input_text=_trace_text(messages[1]["content"], anonymize=anonymize_logs) if store_payloads else None,
                assistant_output_text=_trace_text(response_content, anonymize=anonymize_logs) if store_payloads else None,
                reasoning_summary=_trace_text(reasoning_summary, anonymize=anonymize_logs),
                decision_summary=_trace_text(decision_summary, anonymize=anonymize_logs),
                effective_parameters_json=_trace_json(effective_parameters, anonymize=False),
                provider_metadata_json=_trace_json(
                    {
                        "ok": bool(response.get("ok")),
                        "usage": {
                            "input": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                            "output": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
                            "estimated_cost": usage.get("estimated_cost"),
                        },
                        "error_type": response.get("error_type"),
                        "provider_message": response.get("message"),
                        "input_was_truncated": input_was_truncated,
                        "response_keys": sorted(str(key) for key in response.keys()),
                    },
                    anonymize=anonymize_logs,
                ),
                is_anonymized=anonymize_logs,
            )
        )
    db.commit()
    log_flow_event(
        db,
        company_id=company_id,
        event="ai.completed",
        stage="ai",
        message=f"Ejecución IA finalizada: {purpose}.",
        entity_type="prompt_execution",
        entity_id=execution.id,
        status="success" if validation.ok else "error",
        metadata={
            "purpose": purpose,
            "prompt_name": prompt_name,
            "prompt_version": execution.prompt_version,
            "model": model,
            "output_status": validation.status,
            "validation_errors": validation.errors,
            "input_length": len(text or ""),
            "input_was_truncated": input_was_truncated,
            "output_length": len(response_content or ""),
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "duration_ms": duration_ms,
            "input_reference": input_reference,
            "prompt_execution_id": execution.id,
            **({"effective_parameters": effective_parameters, "decision_summary": decision_summary} if detailed_logging or store_payloads else {}),
        },
    )
    result = dict(response)
    result.update(
        {
            "prompt_execution_id": execution.id,
            "prompt_name": prompt_name,
            "prompt_version": execution.prompt_version,
            "prompt_template_id": execution.prompt_template_id,
            "prompt_purpose": purpose,
            "model": model,
            "parameters": definition.default_parameters,
            "validation_status": validation.status,
            "validation_errors": validation.errors,
            "validation_ok": validation.ok,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "input_reference": input_reference,
            "detail_stored": bool(detailed_logging or store_payloads),
            "payload_stored": bool(store_payloads),
            "decision_summary": decision_summary,
            "reasoning_summary": reasoning_summary,
        }
    )
    if user_id is not None:
        result["user_id"] = user_id
    if validation.ok:
        result["validated_content"] = validation.data
    return result
