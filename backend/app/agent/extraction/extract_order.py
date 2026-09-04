from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from time import perf_counter
from typing import Any, Callable

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, model_supports_reasoning, resolve_reasoning_effort
from app.agent.extraction.prompts import ORDER_EXTRACTION_SYSTEM_PROMPT
from app.agent.extraction.schema import (
    ORDER_EXTRACTION_SCHEMA_VERSION,
    OrderExtraction,
    OrderExtractionInput,
    OrderExtractionResult,
    assert_no_erp_identifiers,
    order_extraction_json_schema,
)


DEFAULT_ORDER_EXTRACTION_MODEL = DEFAULT_OPENAI_MODEL
logger = logging.getLogger(__name__)


class OrderExtractionError(RuntimeError):
    pass


def extract_order(
    input_data: OrderExtractionInput | dict[str, Any],
    *,
    client: Any | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    reasoning_effort: str | None = None,
    input_reference: str | None = None,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> OrderExtractionResult:
    extraction_input = input_data if isinstance(input_data, OrderExtractionInput) else OrderExtractionInput.model_validate(input_data)
    selected_model = model or os.getenv("OPENAI_DEFAULT_MODEL") or DEFAULT_ORDER_EXTRACTION_MODEL
    user_input = extraction_input.combined_text()
    transport = "sdk" if client else "http"
    started = perf_counter()
    content: str | None = None
    try:
        content = _call_structured_extraction(client, extraction_input, selected_model, reasoning_effort=reasoning_effort) if client else _call_structured_extraction_http(extraction_input, selected_model, api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds, reasoning_effort=reasoning_effort)
        payload = _parse_payload(content)
        extracted = OrderExtraction.model_validate(payload)
    except Exception as exc:
        _emit_trace(
            trace_callback,
            {
                "model": selected_model,
                "system_prompt": ORDER_EXTRACTION_SYSTEM_PROMPT,
                "user_input": user_input,
                "assistant_output": content,
                "input_reference": input_reference,
                "duration_ms": int((perf_counter() - started) * 1000),
                "output_status": "provider_error" if content is None else "invalid",
                "validation_errors": ["El proveedor no devolvió una extracción válida."],
                "provider_metadata": {
                    "transport": transport,
                    "error_type": exc.__class__.__name__,
                },
            },
        )
        raise
    _emit_trace(
        trace_callback,
        {
            "model": selected_model,
            "system_prompt": ORDER_EXTRACTION_SYSTEM_PROMPT,
            "user_input": user_input,
            "assistant_output": content,
            "input_reference": input_reference,
            "duration_ms": int((perf_counter() - started) * 1000),
            "output_status": "valid",
            "provider_metadata": {"transport": transport},
            "decision_summary": f"Pedido={extracted.is_order}; líneas={len(extracted.lines)}",
        },
    )
    return OrderExtractionResult(
        rawInput=extraction_input,
        extractedData=extracted,
        model=selected_model,
        schemaVersion=ORDER_EXTRACTION_SCHEMA_VERSION,
    )


def _emit_trace(trace_callback: Callable[[dict[str, Any]], None] | None, trace: dict[str, Any]) -> None:
    if trace_callback is None:
        return
    try:
        trace_callback(trace)
    except Exception:  # pragma: no cover - tracing must never break extraction
        logger.exception("No se pudo persistir la traza de extracción estructurada.")


def _call_structured_extraction_http(
    extraction_input: OrderExtractionInput,
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_api_key:
        raise OrderExtractionError("OPENAI_API_KEY no configurada para extraccion de pedidos.")
    resolved_base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    timeout = timeout_seconds or int(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
    payload = _structured_extraction_payload(extraction_input, model, reasoning_effort=reasoning_effort)
    request = urllib.request.Request(
        f"{resolved_base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {resolved_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(errors="ignore")
        finally:
            exc.close()
        raise OrderExtractionError(f"OpenAI devolvio HTTP {exc.code}: {detail[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise OrderExtractionError(f"No se pudo conectar con OpenAI: {exc}") from exc
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OrderExtractionError("OpenAI no devolvio contenido estructurado.") from exc


def _call_structured_extraction(
    client: Any,
    extraction_input: OrderExtractionInput,
    model: str,
    *,
    reasoning_effort: str | None = None,
) -> str:
    response = client.chat.completions.create(
        **_structured_extraction_payload(extraction_input, model, reasoning_effort=reasoning_effort),
    )
    try:
        message = response.choices[0].message
        content = message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise OrderExtractionError("El proveedor IA no devolvio contenido estructurado.") from exc
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str) or not content.strip():
        raise OrderExtractionError("El proveedor IA devolvio una respuesta vacia.")
    return content


def _structured_extraction_payload(extraction_input: OrderExtractionInput, model: str, *, reasoning_effort: str | None = None) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": ORDER_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": extraction_input.combined_text()},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "order_extraction",
                "schema": order_extraction_json_schema(),
                "strict": True,
            },
        },
    }
    if not model_supports_reasoning(model):
        payload["temperature"] = 0
    resolved_reasoning_effort = resolve_reasoning_effort(reasoning_effort, model)
    if resolved_reasoning_effort:
        payload["reasoning_effort"] = resolved_reasoning_effort
    return payload


def _parse_payload(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OrderExtractionError("El proveedor IA devolvio JSON no valido.") from exc
    if not isinstance(payload, dict):
        raise OrderExtractionError("La extraccion debe ser un objeto JSON.")
    assert_no_erp_identifiers(payload)
    return payload
