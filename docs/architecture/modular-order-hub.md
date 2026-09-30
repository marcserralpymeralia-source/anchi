# Modular Order Hub — estado de BLOQUE 1

Fecha: 2026-09-30
Estado: implementado localmente en `feat/modular-order-hub`; sin conectores externos reales.

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
FakeERPConnector → FAKE-ORDER-000001
```

Los inputs estructurados no calculan confidence semántico: `extraction_confidence` queda en cero y la validación depende de identidad, producto y cantidad. Un pedido estructurado validado no queda bloqueado por el umbral de scoring semántico.

Los inputs desconocidos producen `pending_review` y razones explícitas; no crean un pedido externo.

## Contratos implementados

En `backend/app/connectors/contracts.py`:

- `BaseConnector`: `identifier`, `capabilities`, `health` y validación de configuración.
- `InboundConnector`: transforma payload externo en `InboundOrderEvent`.
- `DestinationConnector`: valida y crea/reproduce idempotentemente un pedido externo.
- `ConnectorCapability`: enum tipado para clientes, productos, precios, inventario, pedidos y catálogo.
- `ConnectorRegistry`: registro local sin instanciar servicios externos.
- `InputMode`: `structured` y `unstructured`.

En `backend/app/connectors/fake.py`:

- `FakeInboundConnector`: acepta payload estructurado local y determinista.
- `FakeERPConnector`: lookup local de clientes/productos y creación idempotente `FAKE-ORDER-*`.

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

## Fiabilidad incluida y pendiente

Incluido en este bloque:

- `InboundMessage` conserva el evento de entrada y su raw payload.
- Dedupe por tenant, provider y `source_external_id` del mensaje.
- `Order.source_external_event_id` evita volver a crear el pedido interno.
- `ExternalIdentity` tiene unicidad tenant/sistema/tipo/ID.
- El fake destination reutiliza el mismo external order ID ante un segundo intento.
- Estado de exportación e intento básico en el pedido.

Preparado pero no implementado aquí:

- outbox transaccional;
- retries con backoff/jitter;
- DLQ y replay operativo;
- circuit breaker y bulkheads;
- reconciliación por cursor o `updated_at`;
- delivery-attempts persistentes separados de `ExportJob`.

## Migración

Se añadió la migración incremental `2026.09.30.1 — tenant modular order hub` en `backend/app/migrations/registry.py`.

La migración:

- añade campos nuevos a `orders` y `order_lines` sin editar migraciones históricas;
- crea `external_identities` y `order_routes` si no existen;
- crea índices únicos tenant-scoped;
- es compatible con SQLite local y con el ledger de migraciones existente.

No se conectan bases reales ni se ejecutan migraciones productivas.

## Compatibilidad

- Email: pipeline existente intacto; sigue usando extracción y scoring.
- WhatsApp conversacional: pipeline y rutas existentes intactos.
- Workbench: sigue mostrando `Order` existente; no se añadió una UI paralela.
- Matching: el flujo estructurado usa resolución determinista por identidad externa; email/WhatsApp siguen usando sus servicios actuales.
- Multi-tenancy: todas las resoluciones nuevas filtran por `company_id` y las rutas son tenant-scoped.

## Planificado

- Adaptar resultados de email/WhatsApp al contrato neutral sin reescribirlos.
- Añadir un input estructurado real, comenzando por WhatsApp Catalog o una API sandbox.
- Añadir un output connector real en sandbox.
- Desarrollar reliability foundation completa antes de producción.
- Añadir configuración/UI de routing solo cuando exista un caso real.
