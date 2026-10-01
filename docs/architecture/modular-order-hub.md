# Modular Order Hub — estado de BLOQUE 5

Fecha: 2026-09-30
Estado: BLOQUE 5 implementado localmente en `feat/modular-order-hub`; Sage 200 ES usa solo transporte mock y no hay HTTP externo real.

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
OrderRoute por tenant y `destination_connection_id`
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

El PoC de Sage conserva el mismo flujo y cambia únicamente el destino registrado explícitamente:

```text
WhatsApp Catalog fixture → WhatsAppCatalogInboundConnector
        → Order / ExternalIdentity → OrderDelivery
        → Sage200ESConnector (`sage_200_es`)
        → Sage200ESClient → MockSage200ESTransport
        → SAGE-ORDER-000001 → Order exported
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

En `backend/app/connectors/sage200es.py`:

- `Sage200ESConnector` es explícito y tenant-scoped; no existe un `SageConnector` genérico.
- `Sage200ESClient` separa la lógica del adapter del transporte.
- `MockSage200ESTransport` simula lookup de sites/clientes/productos, pricing, creación, errores y reconciliación.
- `Sage200ESSalesOrderMapper` solo convierte `Order` a un DTO conceptual marcado `UNVERIFIED_SAGE_CONTRACT`.
- `Sage200ESConfig.real_http_enabled` es `False` por defecto y rechaza HTTP real en esta fase.
- `capabilities` permanece vacío para impedir runtime enablement; `mock_capabilities` documenta el alcance del fixture.

El contrato está preparado para nuevos conectores. Sage 200 ES tiene un PoC local mock; SAP, Odoo, Meta, Shopify, WooCommerce y Salesforce siguen planificados.

## Identidades externas

`ExternalIdentity` es una tabla mínima y tenant-scoped con:

- `company_id`;
- `entity_type` y `entity_id` internos;
- `external_system`;
- `external_id`;
- `external_subtype` y metadata opcional.

La unicidad es `(company_id, entity_type, external_system, external_id)`. Una identidad del tenant A nunca resuelve una entidad del tenant B. `CustomerAlias` y `ProductAlias` se mantienen para aliases locales; no se reutilizan para representar múltiples sistemas externos.

## Routing

`OrderRoute` permite una ruta activa por tenant y tipo de pedido. Las nuevas rutas guardan `destination_connection_id` (y opcionalmente `source_connection_id`) además del `destination_connector_key` legacy. Así dos conexiones Sage o FakeERP del mismo tenant no colisionan. Las rutas antiguas por connector key siguen resolviéndose sin migración destructiva.

El destino por conexión solo se procesa si la conexión pertenece al tenant y está `ACTIVE`. La entrega lazy resuelve exactamente esa conexión; una conexión `DISABLED` no puede recibir eventos, ser elegida por routing ni procesar un outbox pendiente.

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
- La clave estable es `tenant/order/order_id/destination[/connection_id]`; el sufijo evita colisiones cuando hay varias instancias del mismo connector.
- Un único `BackgroundJob` puede reclamar una entrega mediante actualización atómica y lock.
- Si el destino creó el pedido pero la respuesta se perdió, `find_order_by_idempotency_key()` reconcilia antes de crear otro.

Esto proporciona entrega effectively-once/idempotente para destinos que soportan búsqueda por referencia. No afirma exactly-once frente a un ERP que no exponga idempotencia o reconciliación.

## Failure categories

- Retryable: timeout, conexión, indisponibilidad temporal, rate limit.
- Non-retryable: configuración inválida, connector inexistente, pedido inválido o validación permanente.
- Terminal: máximo de intentos agotado; el pedido sigue revisable y recuperable.

## Reconciliation strategy

FakeERP demuestra `remote success + local timeout`: crea una sola orden remota, simula timeout y la siguiente entrega recupera el mismo `FAKE-ORDER-*` sin duplicar. Sage mock demuestra el mismo caso con `SAGE-ORDER-*`, siempre fuera de HTTP real. Otros ERPs podrían necesitar búsqueda por referencia externa o reconciliación periódica por `updated_at`.

## Migración

Se añadió la migración incremental `2026.09.30.1 — tenant modular order hub` en `backend/app/migrations/registry.py`.

La migración:

- añade campos nuevos a `orders` y `order_lines` sin editar migraciones históricas;
- crea `external_identities` y `order_routes` si no existen;
- crea índices únicos tenant-scoped;
- es compatible con SQLite local y con el ledger de migraciones existente.

No se conectan bases reales ni se ejecutan migraciones productivas.

La migración posterior `2026.09.30.2 — tenant order delivery reliability` crea `order_deliveries` y `order_delivery_attempts` con índices únicos tenant-scoped.

La migración posterior `2026.09.30.3 — tenant whatsapp catalog inbound` añade `orders.source_catalog_id` sin modificar migraciones anteriores. No crea una jerarquía de catálogos ni sincroniza datos con Meta.

La migración `2026.09.30.4 — tenant integration registry` crea `integration_connections` y `tenant_integration_roles`, y añade referencias opcionales de conexión a inbound messages, orders, routes y deliveries.

## Compatibilidad

- Email: pipeline existente intacto; sigue usando extracción y scoring.
- WhatsApp conversacional: pipeline y rutas existentes intactos.
- Workbench: sigue mostrando `Order` existente; no se añadió una UI paralela.
- Matching: el flujo estructurado usa resolución determinista por identidad externa; email/WhatsApp siguen usando sus servicios actuales.
- Multi-tenancy: todas las resoluciones nuevas filtran por `company_id` y las rutas son tenant-scoped.
- Integraciones: WhatsApp/email reutilizan su configuración tenant-scoped legacy; la nueva conexión guarda solo configuración no secreta y deja una frontera explícita para un futuro SecretProvider.
- Lazy loading: listar definitions no importa ni instancia adapters; el factory se importa únicamente al resolver una conexión activa o ejecutar su health on-demand.
- El procesamiento inbound no depende de la disponibilidad del destino; un fallo de connector A no bloquea connector B.

## WhatsApp Catalog inbound

WhatsApp Catalog reutiliza el webhook, la verificación de token/firma, el límite de 512 KiB, la resolución de tenant por WABA/phone number ID, la persistencia de `InboundMessage` y la deduplicación existentes. No se creó un segundo endpoint Meta.

El clasificador existente reconoce `type=order` y entrega el evento a `WhatsAppCatalogInboundConnector`. El connector solo adapta el payload a `InboundOrderEvent`; no crea `Order`, no conoce FakeERP, no llama a OpenAI y no hace matching semántico.

| Meta | ANCHI |
|---|---|
| `messages[].id` (`wamid`) | `Order.source_external_event_id` y dedupe inbound con provider `meta` |
| `messages[].from` | `ExternalIdentity(meta_whatsapp, wa_id)`; fallback al matching determinista por teléfono |
| `order.catalog_id` | `Order.source_catalog_id`; además debe coincidir con el catálogo autorizado del tenant |
| `product_items[].product_retailer_id` | `ExternalIdentity(meta_whatsapp_catalog, retailer_id)` y `OrderLine.source_product_external_id` |
| `product_items[].quantity` | `OrderLine.quantity`, convertido y validado determinísticamente |
| `item_price` / `currency` | `OrderLine.unit_price_received` / `currency_received` como source price |
| `order.text` | `Order.notes`, sin interpretación semántica |

La configuración local separa `catalog_enabled`, `catalog_orders_enabled` y `catalog_id`. Un evento de catálogo sin esas tres condiciones, o con otro `catalog_id`, queda trazable en Inbox como revisión y no crea `OrderDelivery`. Las identidades y las órdenes filtran siempre por `company_id`; un mapping de otro tenant no puede resolverse.

El flujo válido es:

```text
Meta fixture → webhook existente → InboundMessage
             → WhatsAppCatalogInboundConnector
             → ExternalIdentity determinista
             → Order / OrderLine validated
             → OrderRoute → OrderDelivery
             → FakeERPConnector → FAKE-ORDER-000001
```

El flujo Sage del PoC es:

```text
Meta fixture → webhook/adapter existente → InboundMessage
             → WhatsAppCatalogInboundConnector
             → ExternalIdentity determinista
             → Order / OrderLine validated
             → OrderRoute → OrderDelivery
             → Sage200ESConnector → MockSage200ESTransport
             → SAGE-ORDER-000001
```

Un `wamid` repetido produce un único `InboundMessage`, `Order`, `OrderDelivery` y job de entrega. Los `text`, `image`, `document`, `audio`, `interactive` y demás tipos siguen en el pipeline conversacional existente. `context.referred_product` se conserva en el payload auditado y no se convierte en pedido.

Las cantidades cero, negativas, no numéricas o no finitas, las monedas inválidas y las órdenes con monedas mezcladas quedan en revisión controlada. Un cliente o producto desconocido deja la orden en `pending_review`, sin exportación automática. `item_price` no sustituye todavía al precio autoritativo del ERP: FakeERP acepta el source price en esta fase y un futuro conector Sage/Odoo/SAP podrá recalcularlo.

## Pending reliability work

- Circuit breaker `healthy → degraded/open → half-open` si un connector real lo requiere.
- Bulkheads y límites de concurrencia por connector.
- UI avanzada de DLQ y replay.
- Reconciliación por cursor/`updated_at` para destinos sin idempotency key directa.

## Fuera de este bloque

- No hay outbound Catalog, respuestas automáticas de producto ni confirmación de pedido por Meta.
- No hay sincronización ERP → Meta Catalog ni llamadas a `whatsapp_commerce_settings`.
- No hay ERP real ni HTTP Sage: el destino Sage del PoC es `MockSage200ESTransport` y el destino legacy sigue siendo FakeERP.
- No se añadió UI de mapping; los helpers de `ExternalIdentity` permiten seeds y operaciones futuras.

## Siguiente fase

BLOQUE 4C será la primera validación contra una instancia Sage 200 real/controlada. Requiere producto exacto, versión, Advanced, Soluciones Conectadas activa, site, credenciales/API y autorización explícita. No se debe habilitar `HTTPSage200ESTransport` ni HTTP real antes de cerrar el informe de contract verification.
