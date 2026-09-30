# Modular Order Hub — estado de BLOQUE 2

Fecha: 2026-09-30
Estado: BLOQUE 2 implementado localmente en `feat/modular-order-hub`; sin conectores externos reales.

## Decisión de dominio

`Order` sigue siendo la entidad canónica interna. No se creó `canonical_orders` ni una segunda tabla de pedidos.

Se conservaron `OrderLine`, `InboundMessage`, `NormalizedInput`, clientes, productos, aliases, scoring, `OrderReview`, workbench y exportaciones existentes. El nuevo contrato añade a `Order` el origen neutral, el modo de entrada, la validación/revisión y el destino/exportación. `OrderLine` conserva la referencia interna y añade identidad de producto externo y precio recibido.

Los pedidos históricos de email y WhatsApp siguen usando sus campos y estados existentes. Los campos nuevos son compatibles y tienen valores por defecto; los pipelines maduros no se reescribieron.

## Flujo implementado

```text
Fake structured input
        │
        ▼
FakeInboundConnector → InboundOrderEvent
        │
        ▼
InboundMessage + external event deduplication
        │
        ▼
Tenant-scoped ExternalIdentity resolution
        │
        ▼
ANCHI Order + OrderLine
        │
        ▼
Deterministic validation (sin agente/LLM)
        │
        ▼
OrderRoute por tenant
        │
        ▼
OrderDelivery / transactional outbox
        │
        ▼
BackgroundJob `deliver_order` → delivery worker
        │
        ▼
FakeERPConnector → FAKE-ORDER-000001
```

Los inputs estructurados no calculan confidence semántico: `extraction_confidence` queda en cero y la validación depende de identidad, producto y cantidad. Un pedido estructurado validado no queda bloqueado por el umbral de scoring semántico.

Los inputs desconocidos producen `pending_review` y razones explícitas; no crean un pedido externo. Un pedido validado pasa a `ready_to_export` y la entrega queda separada del procesamiento inbound.

## Contratos implementados

En `backend/app/connectors/contracts.py`:

- `BaseConnector`: `identifier`, `capabilities`, `health` y validación de configuración.
- `InboundConnector`: transforma payload externo en `InboundOrderEvent`.
- `DestinationConnector`: valida, crea/reproduce idempotentemente y puede reconciliar por idempotency key.
- `ConnectorDeliveryError`: normaliza fallos temporales, permanentes y de resultado remoto desconocido.
- `ConnectorCapability`: enum tipado para clientes, productos, precios, inventario, pedidos y catálogo.
- `ConnectorRegistry`: registro local sin instanciar servicios externos.
- `InputMode`: `structured` y `unstructured`.

En `backend/app/connectors/fake.py`:

- `FakeInboundConnector`: acepta payload estructurado local y determinista.
- `FakeERPConnector`: lookup local de clientes/productos y creación idempotente `FAKE-ORDER-*`.
- FakeERP permite simular timeout antes/después de crear, indisponibilidad temporal, rate limit, validación permanente y health configurable.

El contrato está preparado para nuevos conectores, pero Sage, SAP, Odoo, Meta, Shopify, WooCommerce y Salesforce siguen planificados.

## Identidades externas

`ExternalIdentity` es una tabla mínima y tenant-scoped con:

- `company_id`;
- `entity_type` y `entity_id` internos;
- `external_system`;
- `external_id`;
- `external_subtype` y metadata opcional.

La unicidad es `(company_id, entity_type, external_system, external_id)`. Una identidad del tenant A nunca resuelve una entidad del tenant B. `CustomerAlias` y `ProductAlias` se mantienen para aliases locales; no se reutilizan para representar múltiples sistemas externos.

## Routing

`OrderRoute` permite una ruta activa por tenant y tipo de pedido. En este bloque el destino de prueba es `order → fake_erp`. El router solo conoce el connector key y el contrato; no conoce campos de Sage, Shopify u Odoo.

El resultado se guarda en `Order.destination_external_order_id`, junto con `destination_connector_key`, `export_status`, `export_attempt_count` y `export_last_error`.

## Delivery lifecycle

```text
validated → ready_to_export → delivering → exported
                         └──→ retrying → delivering
                         └──→ failed / error_exportacion
```

`ExportJob` se conserva para exportaciones históricas de archivos/FTP. La nueva entrega reutiliza `BackgroundJob` para cola, dedupe, claim atómico, locks, recuperación de jobs stale, tenant scope y finalización; `OrderDelivery` es el outbox de dominio porque `ExportJob` tiene semántica de archivo distinta.

`OrderDeliveryAttempt` conserva cada intento, error, backoff y resultado sin guardar payloads completos ni secretos.

## Retry semantics

Timeouts, conexión, indisponibilidad temporal y rate limit son retryable. Configuración inválida, connector inexistente, pedido inválido y validación permanente son terminales. El backoff es exponencial, acotado y determinista; los tests no usan sleeps reales.

Al agotar el máximo de intentos, el outbox queda `failed`, el pedido queda en `export_failed`/`error_exportacion` y el error permanece revisable. `retry_order_delivery()` permite reabrirlo explícitamente sin crear otra intención.

## Idempotency guarantees

- Inbound: tenant + provider + external event ID evita otro `Order`.
- Outbound: `(tenant, idempotency_key)` tiene unicidad de base de datos.
- La clave estable es `tenant/order/order_id/destination`.
- Un único `BackgroundJob` puede reclamar una entrega mediante actualización atómica y lock.
- Si el destino creó el pedido pero la respuesta se perdió, `find_order_by_idempotency_key()` reconcilia antes de crear otro.

Esto proporciona entrega effectively-once/idempotente para destinos que soportan búsqueda por referencia. No afirma exactly-once frente a un ERP que no exponga idempotencia o reconciliación.

## Failure categories

- Retryable: timeout, conexión, indisponibilidad temporal, rate limit.
- Non-retryable: configuración inválida, connector inexistente, pedido inválido o validación permanente.
- Terminal: máximo de intentos agotado; el pedido sigue revisable y recuperable.

## Reconciliation strategy

FakeERP demuestra `remote success + local timeout`: crea una sola orden remota, simula timeout y la siguiente entrega recupera el mismo `FAKE-ORDER-*` sin duplicar. Otros ERPs podrían necesitar búsqueda por referencia externa o reconciliación periódica por `updated_at`.

## Migración

Se añadió la migración incremental `2026.09.30.1 — tenant modular order hub` en `backend/app/migrations/registry.py`.

La migración:

- añade campos nuevos a `orders` y `order_lines` sin editar migraciones históricas;
- crea `external_identities` y `order_routes` si no existen;
- crea índices únicos tenant-scoped;
- es compatible con SQLite local y con el ledger de migraciones existente.

No se conectan bases reales ni se ejecutan migraciones productivas.

La migración posterior `2026.09.30.2 — tenant order delivery reliability` crea `order_deliveries` y `order_delivery_attempts` con índices únicos tenant-scoped.

## Compatibilidad

- Email: pipeline existente intacto; sigue usando extracción y scoring.
- WhatsApp conversacional: pipeline y rutas existentes intactos.
- Workbench: sigue mostrando `Order` existente; no se añadió una UI paralela.
- Matching: el flujo estructurado usa resolución determinista por identidad externa; email/WhatsApp siguen usando sus servicios actuales.
- Multi-tenancy: todas las resoluciones nuevas filtran por `company_id` y las rutas son tenant-scoped.
- El procesamiento inbound no depende de la disponibilidad del destino; un fallo de connector A no bloquea connector B.

## Pending reliability work

- Circuit breaker `healthy → degraded/open → half-open` si un connector real lo requiere.
- Bulkheads y límites de concurrencia por connector.
- UI avanzada de DLQ y replay.
- Reconciliación por cursor/`updated_at` para destinos sin idempotency key directa.

## Planificado

- Adaptar resultados de email/WhatsApp al contrato neutral sin reescribirlos.
- Añadir un input estructurado real, comenzando por WhatsApp Catalog o una API sandbox.
- Añadir un output connector real en sandbox.
- Añadir configuración/UI de routing solo cuando exista un caso real.
