# Arquitectura preliminar del Modular Order Hub

Fecha: 2026-09-30  
Estado: propuesta de BLOQUE 0.5; no implementada.

## Flujo objetivo

```text
INPUT CONNECTORS
  ├─ Email / IMAP
  ├─ WhatsApp Cloud API
  ├─ Web / portal / API
  ├─ Ficheros y batch
  └─ Futuras redes/social/voz
        │
        ▼
INBOX + EVIDENCE STORE
  firma, delivery_id, raw payload, attachments, dedupe, tenant
        │
        ▼
STRUCTURED / UNSTRUCTURED PROCESSING
  ├─ Structured: catalog order, API JSON, CSV, ERP document
  └─ Unstructured: text, PDF, image/OCR, audio/STT
        │
        ▼
CANONICAL DOMAIN
  customer, product, order, order_line, provenance, external identities
        │
        ▼
VALIDATION
  schema, tenant, identity, product, quantity, policy, confidence
        │
        ▼
WORKBENCH
  review, correction, evidence, audit, approval, learning
        │
        ▼
ROUTING
  capability matrix, tenant config, destination policy, idempotency key
        │
        ▼
OUTPUT CONNECTORS
  WooCommerce · Shopify · Odoo · SAP B1 · Sage · Salesforce · CSV/FTP/SFTP
        │
        └──────────────► OUTBOX / DELIVERY ATTEMPTS / RECONCILIATION
```

## Qué es actual, qué es nuevo y qué es externo

| Bloque | Estado en ANCHI | Propuesta |
|---|---|---|
| Email/IMAP | Actual | Mantener `EmailIngestionService` y workers; normalizar a inbox común |
| WhatsApp webhook y envío | Actual en `app/whatsapp`; stub en el registry genérico | Envolver el servicio real en un `InputConnector` con capabilities y dedupe |
| Web/portal/API/social/voz | Stub o no implementado | Nuevos conectores cuando exista un caso; no bloquear el dominio por ellos |
| Inbox/evidence | Parcial: `InboundMessage`, attachments y raw payload | Añadir evento de entrega, firma, hash, delivery ID y estado de procesamiento |
| Structured processing | Nuevo | Parseadores deterministas para pedido de catálogo, JSON, CSV y documentos ERP |
| Unstructured processing | Parcial: normalización, placeholders PDF/OCR/STT | Interfaces de extracción con evidencia, versión de extractor y error recuperable |
| Canonical Domain | Actual: `Order`, `OrderLine`, customer/product/matching | Extender con provenance, external identities, snapshots y estados canónicos |
| Validation | Actual: scoring, matching, review | Separar validación de extracción; razones y reglas trazables |
| Workbench | Actual | Mantener como punto de decisión humana, no desplazarlo a un ERP externo |
| Routing | Parcial: exportación configurable | Nuevo routing por tenant, tipo de operación, capability y estado de aprobación |
| Output connectors | Parcial: CSV/FTP/SFTP | Nuevos adaptadores con contrato común; empezar en mock/sandbox |
| Outbox/attempts/DLQ | Parcial: jobs/export jobs con retries | Nuevo subsistema transversal, preferiblemente SQL-first antes de un broker externo |
| Reconciliación | No observada como capacidad común | Nuevo proceso por cursor, `updated_at`, replay o importación de diferencias |

## Contratos conceptuales

### InputConnector

Cada conector de entrada debería declarar al menos:

- `connector_key`, `provider`, `tenant/company_id` y versión de esquema.
- `verify()` y `health_check()`.
- `receive(raw_request)`: valida autenticidad y devuelve un receipt rápido.
- `fetch()` para canales pull.
- `normalize(receipt)`: produce una evidencia común sin perder el raw payload.
- `acknowledge()` o estado equivalente del proveedor.
- capabilities: texto, media, audio, documentos, structured orders, status events, replay.

`receive()` no debe ejecutar matching, OCR, LLM ni exportación. Debe validar lo mínimo, persistir/encolar el receipt y devolver 2xx/202 dentro del SLA del proveedor.

### Processing contract

El procesamiento debe diferenciar:

1. **Evidence**: payload y archivos originales inmutables, con hash y referencia a proveedor.
2. **Interpretation**: texto normalizado, extracción estructurada, OCR/STT, clasificación y matching.
3. **Decision**: score, validación, revisión humana y aprobación.

Una corrección nunca debe sobreescribir la evidencia original. Debe registrar actor, motivo, valor anterior, valor corregido y si puede enseñar al sistema.

### OutputConnector

Cada salida debería declarar:

- `connector_key`, proveedor, tenant y versión de API.
- operaciones soportadas: create, upsert, update, cancel, status, stock o document.
- auth/session lifecycle y health check.
- `send(command, idempotency_key)`.
- clasificación de errores: validación, autenticación, rate limit, timeout, conflicto, proveedor caído, error permanente.
- external ID resultante y request/response auditado sin secretos.
- `reconcile(window/cursor)`.

El router nunca debe conocer campos de SAP, Shopify u Odoo. Debe entregar un comando canónico al conector y recibir un resultado canónico.

## Fiabilidad transversal

La semántica recomendada es **at-least-once con efectos idempotentes**, no exactly-once global.

### Entrada

- Validar firma sobre el cuerpo crudo antes de interpretar.
- Formar una clave estable: `tenant + provider + delivery_id/event_id`; si no existe, usar combinación documentada de fuente y hash.
- Constraint única para el receipt y respuesta idempotente a duplicados.
- Persistir el raw payload antes del trabajo pesado.
- Mantener estado `received → queued → processing → processed/failed/dead_lettered`.

### Salida

- Crear dominio y outbox en la misma transacción SQL.
- Cada outbox record incluye `aggregate_id`, operación, destino, payload versionado, `idempotency_key` y next attempt.
- El worker reclama con lock/lease, respeta rate limits y aplica backoff exponencial con jitter.
- Guardar cada attempt, status HTTP/proveedor, request ID externo y error clasificado.
- Circuit breaker por tenant/proveedor para evitar tormentas; bulkhead para no bloquear otros tenants.
- Tras agotar política, DLQ con replay manual/automático y razón visible en workbench.

### Reconciliación

La reconciliación no es un parche: es necesaria porque proveedores pueden perder eventos, reordenarlos, deshabilitar webhooks o retenerlos solo un tiempo. Debe ser periódica y por capability:

- Shopify/Woo: `updated_at`, delivery IDs y ventanas solapadas.
- Salesforce: ReplayId durante retención más consulta por External ID.
- SAP/Odoo/Sage: cursores o filtros por fecha/modificación según API.
- WhatsApp: relectura de logs/estado cuando el proveedor lo permita; nunca asumir que el webhook es el único registro.

## Mapeo con el código actual

- Entrada común: `backend/app/agent/platform.py:67-170` (`InboundPayload`, `PipelineResult`, registry y stubs).
- WhatsApp operativo: `backend/app/whatsapp/routes.py:438-767` y `backend/app/whatsapp/service.py:1276-1280`.
- Evidencia: `backend/app/db/models.py:773-924` (`InboundMessage`, attachments, normalized input, review y correcciones).
- Jobs fiables ya iniciados: `backend/app/db/models.py:967-990` (`dedupe_key`, attempts, retries, lock y next retry).
- Dominio: `backend/app/db/models.py:1100-1175` (`Order`, `OrderLine`).
- Audit: `backend/app/db/models.py:1198-1208`.
- Workbench: `backend/app/workbench/routes.py` y templates relacionados.
- Exportación actual: `backend/app/exports/service.py` y `ExportJob`.

## Secuencia propuesta para BLOQUE 1

1. Definir contratos y estados sin cambiar todavía proveedores reales.
2. Añadir fixtures locales para webhook WhatsApp, mensaje de texto, media y structured order.
3. Implementar inbox/dedupe/outbox sobre los modelos existentes, manteniendo compatibilidad con `BackgroundJob`.
4. Producir un `CanonicalOrder` desde dos entradas: texto libre y pedido estructurado, conservando provenance.
5. Ejecutar validación y workbench con el mismo flujo actual.
6. Crear un output connector mock que pruebe idempotencia, retries, DLQ y reconciliation.
7. Solo después habilitar una cuenta sandbox de un proveedor, con credenciales de prueba y sin datos de producción.

## Límites de esta propuesta

- No selecciona broker, proveedor de OCR/STT, motor LLM, ERP concreto ni estrategia de despliegue.
- No autoriza migraciones, cambios de producción, conexión a Meta/ERP/commerce, ni instalación de librerías.
- No convierte los repositorios open source estudiados en dependencias de ANCHI.

## Proveniencia

La arquitectura combina el `ARCHITECTURE.md` y los modelos actuales de ANCHI con patrones documentados en [AWS cloud patterns](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/introduction.html), [AWS transactional outbox](https://aws.amazon.com/blogs/compute/implementing-the-transactional-outbox-pattern-with-amazon-eventbridge-pipes/), [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests), [GitHub webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks), [Microsoft Graph delivery](https://learn.microsoft.com/en-au/graph/change-notifications-delivery-webhooks), [WooCommerce webhooks](https://developer.woocommerce.com/docs/apis/rest-api/v2/webhooks), [Shopify webhook verification](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries), [Salesforce Pub/Sub](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/intro.html) y las fuentes de integración enumeradas en [open-source-registry.md](./open-source-registry.md). Consultado el 2026-09-30.
