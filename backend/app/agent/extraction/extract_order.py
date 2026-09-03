from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL
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
) -> OrderExtractionResult:
    extraction_input = input_data if isinstance(input_data, OrderExtractionInput) else OrderExtractionInput.model_validate(input_data)
    selected_model = model or os.getenv("OPENAI_DEFAULT_MODEL") or DEFAULT_ORDER_EXTRACTION_MODEL
    content = _call_structured_extraction(client, extraction_input, selected_model) if client else _call_structured_extraction_http(extraction_input, selected_model, api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)
    payload = _parse_payload(content)
    extracted = OrderExtraction.model_validate(payload)
    return OrderExtractionResult(
        rawInput=extraction_input,
        extractedData=extracted,
        model=selected_model,
        schemaVersion=ORDER_EXTRACTION_SCHEMA_VERSION,
    )


def _call_structured_extraction_http(extraction_input: OrderExtractionInput, model: str, *, api_key: str | None = None, base_url: str | None = None, timeout_seconds: int | None = None) -> str:
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_api_key:
        raise OrderExtractionError("OPENAI_API_KEY no configurada para extraccion de pedidos.")
    resolved_base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    timeout = timeout_seconds or int(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
    payload = _structured_extraction_payload(extraction_input, model)
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


def _call_structured_extraction(client: Any, extraction_input: OrderExtractionInput, model: str) -> str:
    response = client.chat.completions.create(
        **_structured_extraction_payload(extraction_input, model),
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


def _structured_extraction_payload(extraction_input: OrderExtractionInput, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": ORDER_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": extraction_input.combined_text()},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "order_extraction",
                "schema": order_extraction_json_schema(),
                "strict": True,
            },
        },
    }


def _parse_payload(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OrderExtractionError("El proveedor IA devolvio JSON no valido.") from exc
    if not isinstance(payload, dict):
        raise OrderExtractionError("La extraccion debe ser un objeto JSON.")
    assert_no_erp_identifiers(payload)
    return payload
