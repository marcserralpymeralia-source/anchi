# Extraccion estructurada de pedidos

## Objetivo

Anchi incorpora una capa aislada para transformar texto no estructurado de pedidos B2B en una estructura auditable antes de cualquier matching contra clientes, productos o Sage.

## Contrato

- Modulo: `app.agent.extraction`.
- Funcion principal: `extract_order(input_data, client=None, model=None)`.
- Version de esquema: `ORDER_EXTRACTION_SCHEMA_VERSION = "1.0"`.
- Entrada preparada para email, PDF, WhatsApp, voz, redes o carga manual.
- Salida: `OrderExtractionResult`, con `rawInput`, `extractedData`, `model`, `timestamp` y `schemaVersion`.

## Reglas

- El LLM interpreta el contenido recibido.
- La app valida el JSON con Pydantic.
- No se permite devolver `customerId`, `productId`, codigos Sage ni referencias ERP.
- Las cantidades solo son numericas cuando se pueden leer con seguridad.
- Los campos desconocidos van como `null` y se registran como incertidumbre si afectan a la revision.
- La funcion no hace matching, scoring, exportacion ni persistencia.

## Validacion

Los tests usan fixtures deterministas y un cliente IA simulado. Esto permite evaluar el contrato sin llamadas reales ni coste de LLM.

## Integracion en pipeline

El pipeline real intenta primero `structured_order_extraction` tras normalizar y clasificar la entrada. Si la extraccion estructurada falla, cae al extractor anterior y deja el motivo resumido en `_extraction_meta.structuredFallbackReason`.

La traza se persiste en `inbound_messages.extraction_json` y se muestra en la revision operativa como `Extractor estructurado` o `Extractor anterior`.
