# Integration Registry y configuración por tenant

Estado: BLOQUE 5, implementado localmente en `feat/modular-order-hub`.

Este diseño separa el catálogo estático de adapters de las conexiones concretas que un tenant decide activar. No es un marketplace: no hay billing, licensing, descarga de plugins, código externo ni sandbox.

## Capas

```text
ConnectorDefinition
        │  tipo disponible, schema y capabilities declaradas
        ▼
IntegrationConnection
        │  instancia/configuración de un tenant
        ▼
OrderRoute / TenantIntegrationRole
        │  destino de pedidos y fuentes de master data
        ▼
lazy connector instance
```

`IntegrationRegistry` vive en `backend/app/connectors/registry.py`. Sus definiciones no contienen estado de tenant y listar el registro no importa ni instancia adapters. Cada definición declara:

- key estable: `whatsapp_catalog`, `email`, `fake_inbound`, `fake_erp`, `sage_200_es`;
- display name, dirección y madurez `stable`/`experimental`;
- capabilities runtime verificadas;
- `mock_capabilities` separadas de las capabilities reales;
- schema de configuración no secreta;
- soporte de health, necesidad de credenciales y source legacy;
- factory lazy y gate de runtime.

Sage 200 ES mantiene `capabilities=[]` porque BLOQUE 4B no verificó el contrato runtime. Sus capabilities mock solo se consideran en `APP_ENV=development/test` y no habilitan HTTP. `real_http_enabled=true` siempre queda bloqueado.

FakeInbound y FakeERP solo aparecen en el listado operativo de `development/test`. No se ofrecen como opciones productivas.

## IntegrationConnection

`integration_connections` es tenant-scoped y permite varias filas con el mismo `connector_key`. Guarda:

- `company_id`, connector key, nombre y lifecycle state;
- `config_json` únicamente con valores no secretos, como site, catalog id, flags y routing props;
- `secret_provider_ref` como frontera futura, sin implementar un vault;
- `requires_credentials` y `credentials_configured` sin persistir credenciales raw;
- último health on-demand.

Lifecycle:

```text
DRAFT → CONFIGURED → ACTIVE → DISABLED
             └──────→ MISCONFIGURED
```

Una configuración incompleta se puede guardar para que el administrador la termine, pero `ACTIVE` exige todos los campos obligatorios, credentials fuera de `config_json` cuando corresponda y los gates del runtime. `DISABLED` no recibe eventos, no se usa en routes, no crea deliveries y no ejecuta health externo.

WhatsApp y email conservan sus tablas/settings tenant-scoped existentes. La IntegrationConnection solo actúa como referencia/adaptación: no copia Meta tokens, contraseñas IMAP, OAuth tokens ni SMTP secrets.

## Master data roles

`tenant_integration_roles` guarda una selección por tenant para:

| Rol | Capability exigida |
|---|---|
| `customers_source` | `customers.read` |
| `products_source` | `products.read` |
| `pricing_source` | `pricing.read` |
| `inventory_source` | `inventory.read` |
| `order_destination` | `orders.create` |

El backend rechaza una selección que no declare la capability requerida. En development/test puede usar las capabilities mock de Sage, siempre que la conexión siga siendo el transporte mock seguro.

Todavía no hay sincronización de clientes, productos, precios o stock; solo se modela la responsabilidad elegida.

## Routing y trazabilidad

`OrderRoute` conserva `destination_connector_key` para backwards compatibility y añade `destination_connection_id` y `source_connection_id`. `OrderDelivery` y `Order` conservan también la conexión de destino. La clave de idempotencia incluye el connection id cuando está disponible, por lo que dos Sage distintos no comparten outbox.

`InboundMessage` y `Order` admiten `source_connection_id`. El flujo structured valida que la conexión de entrada pertenezca al tenant, esté activa y corresponda al connector del evento. Los pipelines legacy siguen funcionando sin rellenar este campo.

## Lazy loading y health

Resolver una conexión sigue siempre esta secuencia:

1. filtrar por `company_id` y `connection_id`;
2. comprobar que está `ACTIVE`;
3. cargar su configuración no secreta;
4. importar la factory del connector;
5. instanciar solo esa conexión.

No se inicializan Sage, FakeERP ni futuros Odoo/Shopify/Salesforce al arrancar ANCHI. El health es on-demand y solo afecta a la conexión solicitada. Para Sage la instancia siempre utiliza `MockSage200ESTransport` en este bloque.

## API interna y UI

Endpoints autenticados y tenant-scoped:

- `GET /integrations/definitions`;
- `GET/POST /integrations/connections`;
- `PATCH /integrations/connections/{id}`;
- `POST /integrations/connections/{id}/enable|disable|validate|health`;
- `GET /integrations/roles` y `PUT /integrations/roles/{role_key}`;
- `GET /integrations/routes` y `PUT /integrations/routes/{order_type}`.

La pantalla `/settings/integrations` ofrece el listado, alta de una conexión con JSON no secreto, activación/desactivación y test on-demand. También muestra el routing y los roles configurados. La edición avanzada de routes/roles se mantiene en la API interna para no introducir un workflow builder.

## Migración y compatibilidad

La migración incremental `2026.09.30.4` crea `integration_connections` y `tenant_integration_roles`, y añade referencias opcionales a los modelos existentes. No edita migraciones anteriores ni toca producción.

Los tenants con `OrderRoute.destination_connector_key` siguen operativos. Los deliveries legacy continúan usando `ConnectorRegistry` y FakeERP. El nuevo runtime por connection utiliza `ConnectionRuntimeRegistry`, que permite resolver dos instancias del mismo connector sin colisión.

## Fuera de scope y deuda

- Sage HTTP real y contrato externo verificado: BLOQUE 4C.
- Secret vault, OAuth flows y rotación de credenciales.
- Odoo, SAP, Salesforce, Shopify, WooCommerce y Generic REST.
- Sincronización de master data.
- UI avanzada de edición de routes/roles y observabilidad continua.
- Marketplace, billing/licensing y carga dinámica de código.

Próximo bloque recomendado: **BLOQUE 4C — validación Sage real controlada**, manteniendo este registry como frontera y sin modificar el core para cada ERP futuro.
