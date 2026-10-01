# Entrega de fase

## 1. Objetivo

Convertir el catálogo de connectors de ANCHI en configuración real por tenant, con lifecycle, capabilities, conexiones múltiples, routing por instancia, roles de master data, health on-demand y carga lazy.

## 2. Alcance ejecutado

- Registry central de `ConnectorDefinition` para WhatsApp Catalog, email, FakeInbound, FakeERP y Sage 200 ES.
- `IntegrationConnection` y `TenantIntegrationRole` tenant-scoped.
- Schema no secreto, `secret_provider_ref` futuro y rechazo de secretos raw.
- States `DRAFT`, `CONFIGURED`, `ACTIVE`, `DISABLED`, `MISCONFIGURED`.
- `OrderRoute`, `OrderDelivery`, `Order`, `InboundMessage` con referencias opcionales a connection.
- Capability validation para roles y destinos.
- Runtime registry lazy compatible con varias conexiones del mismo connector.
- Health check on-demand sin polling ni HTTP Sage.
- API autenticada interna y pantalla mínima `/settings/integrations`.
- Migración incremental `2026.09.30.4`.

## 3. Alcance no ejecutado

- Sage HTTP real y contract verification: BLOQUE 4C.
- Secret vault/OAuth, sincronización de master data y connectors futuros.
- Workflow builder y UI avanzada de routing/roles.
- Marketplace, billing/licensing, plugins externos y producción.

## 4. Diagnostico previo

`OrderRoute` solo guardaba `destination_connector_key`; `InboundMessage` y `Order` no tenían source connection. WhatsApp y email ya disponían de configuración tenant-scoped con secretos cifrados/legacy, por lo que duplicarla habría creado dos fuentes de verdad.

## 5. Cambios realizados

Se añadieron definiciones estáticas sin imports de adapters, factories lazy, servicios de lifecycle/capability/health, API interna y una pantalla Settings compacta. Las rutas legacy siguen siendo válidas; las nuevas incluyen `destination_connection_id` y una clave de idempotencia con connection id.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/connectors/__init__.py` | Evitar imports globales de adapters | refactor lazy |
| `backend/app/connectors/contracts.py` | Trazar source connection en eventos | contrato compatible |
| `backend/app/connectors/routing.py` | Resolver destino por connection | routing |
| `backend/app/connectors/sage200es.py` | Factory segura para Sage mock | factory |
| `backend/app/core/router_registry.py` | Registrar API de integraciones | wiring |
| `backend/app/db/models.py` | Persistir conexiones, roles y referencias | modelo |
| `backend/app/messages/service.py` | Persistir source connection opcional | trazabilidad |
| `backend/app/migrations/registry.py` | Migración incremental .4 | schema |
| `backend/app/orders/delivery.py` | Delivery por connection y disabled gate | runtime |
| `backend/app/orders/modular.py` | Validar source tenant-scoped/activo | seguridad |
| `backend/app/templates/settings/index.html` | Enlace a Integraciones | UI |
| `docs/architecture/modular-order-hub.md` | Actualizar arquitectura | documentación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/connectors/registry.py` | Definitions y resolución lazy |
| `backend/app/integrations/__init__.py` | Paquete de integraciones |
| `backend/app/integrations/service.py` | Lifecycle, roles, routes, health |
| `backend/app/integrations/routes.py` | API interna y página Settings |
| `backend/app/templates/settings/integrations.html` | UI mínima |
| `backend/tests/test_integration_registry.py` | Tests de dominio, aislamiento, lazy y migración |
| `docs/architecture/integration-registry.md` | Diseño y deuda |
| `docs/development/block5-integration-registry-delivery.md` | Entrega de fase |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| `IntegrationConnection` no es única por tenant+connector | Permite dos WhatsApp, Sage o ERP | constraint compuesto |
| `config_json` no contiene secretos | Evita duplicar y exponer credenciales | vault prematuro o plaintext |
| `destination_connection_id` junto a connector key | Backwards compatibility y routing por instancia | migración destructiva |
| `mock_capabilities` separadas | Sage no tiene capabilities runtime verificadas | declarar el PoC como real |
| factory por import path | No cargar SDKs/adapters no usados | inicialización global |
| health on-demand | Evita polling y coste/efectos externos | worker continuo |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python3 -m unittest backend.tests.test_integration_registry -v` | 9 tests OK |
| `python3 -m unittest backend.tests.test_integration_registry backend.tests.test_modular_order_hub backend.tests.test_order_delivery_reliability backend.tests.test_whatsapp_catalog_inbound backend.tests.test_templating_paths -v` | 42 tests OK |
| `python3 -m unittest backend.tests.test_schema_migrations -v` | 24 tests OK |
| `python3 -m unittest backend.tests.test_core backend.tests.test_tenant_isolation backend.tests.test_static_assets -v` | 49 tests OK |
| AST parse de `backend/app/**/*.py` | 154 archivos OK |
| `git diff --check` | OK |
| Import lazy en proceso limpio | Sage/WhatsApp no cargados al listar registry |
| `python3 -m unittest backend.tests.test_modular_order_hub backend.tests.test_order_delivery_reliability backend.tests.test_whatsapp_catalog_inbound backend.tests.test_home_workbench_optimization backend.tests.test_pending_orders_access backend.tests.test_regression_flows` | 60 tests OK; 1 error preexistente por `PermissionError` al escribir el PDF de `test_mock_order_uses_tenant_company_name` |

## 10. Tests añadidos o modificados

`backend/tests/test_integration_registry.py` cubre definitions sin instanciación, visibility de Fake, conexiones duplicadas, lifecycle/config/secrets, aislamiento, roles/capabilities, routing y delivery por instancia, disabled gate, health on-demand, Sage mock seguro y migración incremental.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| Registry central | OK | `IntegrationRegistry` y test de listado |
| Varias conexiones por connector | OK | test de dos FakeERP |
| Secret boundary | OK | raw secret rechazado |
| Tenant isolation | OK | resolve/source/queries filtrados |
| Capability validation | OK | roles y route destination |
| Lazy loading | OK | factory/import test |
| Disabled no procesa | OK | delivery gate y test |
| Health on-demand | OK | test healthy/disabled |
| Sage real HTTP bloqueado | OK | config/factory PoC y test |
| Backwards compatibility | OK | suites modular/reliability/WhatsApp/email/core |

## 12. Riesgos y observaciones pendientes

- La selección de roles todavía no ejecuta sincronización ni lookup de master data.
- La UI avanzada de edición de roles/routes queda en la API interna.
- La configuración de una segunda conexión WhatsApp necesita que el bridge futuro identifique phone/WABA y rellene `source_connection_id`; no se duplicaron secretos.
- El delivery legacy sin connection id sigue dependiendo del `ConnectorRegistry` existente.

## 13. Desviaciones respecto al alcance inicial

La UI se mantuvo mínima y la edición de routes/roles quedó documentada/API-only para evitar refactorizar la configuración legacy de WhatsApp/email dentro de este bloque.

## 14. Estado final de Git

Pendiente de revisión final y commits locales pequeños. No se ha hecho push, no se ha configurado upstream y no se ha tocado producción.

## 15. Recomendacion para la siguiente fase

BLOQUE 4C: validar el contrato Sage contra una instancia real/controlada y reemplazar únicamente la factory/transport seguro después de autorización explícita.
