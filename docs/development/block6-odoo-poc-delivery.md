# Entrega de fase

## 1. Objetivo

Validar que el Integration Registry permite añadir un ERP distinto —Odoo 19+ JSON-2— sin modificar WhatsApp, `Order`, Workbench ni la reliability layer.

## 2. Alcance ejecutado

- Identifier estable `odoo` para Odoo 19+ JSON-2.
- Capas `OdooConnector → OdooClient → OdooTransport`.
- `MockOdooJSON2Transport` sin HTTP externo.
- Fixtures sanitizados de `res.partner`, `product.product`, precios y ventas.
- Mapper puro `OdooSalesOrderMapper` con variantes y trazabilidad de precios.
- ExternalIdentity obligatoria para cliente y variante de producto.
- Escenarios de éxito, faltantes, validación, permisos, rate limit, indisponibilidad, timeout antes/después y reconciliación.
- Registro lazy, conexión tenant-scoped, routing por connection y coexistencia Sage/Odoo.
- Sin migraciones y sin campos `odoo_*` en el dominio.

## 3. Alcance no ejecutado

- HTTP real, autenticación bearer efectiva y acceso a SecretProvider.
- Verificación contra `/doc` de una base cliente.
- `action_confirm`, sincronización de stock y pricing real.
- Compatibilidad XML-RPC/JSON-RPC legacy.

## 4. Diagnóstico previo

El BLOQUE 5 ya ofrecía `ConnectorDefinition`, `IntegrationConnection`, `ExternalIdentity`, `OrderRoute`, `OrderDelivery`, capabilities mock y resolución lazy. Odoo se añadió dentro de esas fronteras; no fue necesario modificar WhatsApp, `Order`, Workbench, workers ni la semántica de retry.

## 5. Cambios realizados

`odoo` queda como definición experimental mock-only. Sus capabilities runtime son vacías porque los fields/métodos de una instalación concreta deben verificarse en `/doc`; las capabilities mock cubren clientes, productos, pricing, pedidos create/read. Inventory no se declara.

El mapper conserva `source_price` y `authoritative_price`, usa `product.product` para variantes y deja el pedido en `draft`. `client_order_ref` es solo una estrategia candidata dentro del fixture, no un contrato productivo.

## 6. Archivos modificados

| Archivo | Motivo | Tipo |
|---|---|---|
| `backend/app/connectors/__init__.py` | exports lazy del adapter Odoo | wiring lazy |
| `backend/app/connectors/registry.py` | definición Odoo y gate mock-only | registry |
| `backend/app/integrations/service.py` | gates genéricos para adapters mock-only | lifecycle |
| `backend/tests/test_integration_registry.py` | incluir Odoo en el catálogo de test | regresión |
| `docs/architecture/integration-registry.md` | registrar Odoo y sus límites | arquitectura |
| `docs/architecture/modular-order-hub.md` | actualizar flujo y estado | arquitectura |
| `docs/research/open-source-registry.md` | registrar decisión de dependencia | investigación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/connectors/odoo.py` | connector, client, transport mock y mapper |
| `backend/app/connectors/odoo_fixtures.py` | fixtures sanitizados |
| `backend/tests/test_odoo_connector.py` | contrato mock, aislamiento, routing y E2E |
| `docs/integrations/odoo-client-discovery-checklist.md` | requisitos del cliente real |
| `docs/integrations/odoo-contract-verification.md` | matriz contract/runtime |
| `docs/development/block6-odoo-poc-delivery.md` | acta de entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativa descartada |
|---|---|---|
| identifier `odoo` | estable y explícito dentro del registry | `erp_connector` ambiguo |
| JSON-2 19+ aislado | dirección actual documentada por Odoo | mezclar XML-RPC/JSON-RPC legacy |
| capabilities reales vacías | falta verificar `/doc` de un tenant | declarar fields estándar como contrato universal |
| capabilities mock separadas | permite probar routing sin falsear soporte productivo | activar runtime real prematuramente |
| `product.product` | conserva la variante concreta | colapsar todo a `product.template` |
| `ExternalIdentity` outbound | evita búsquedas heurísticas | buscar por nombre/teléfono |
| pedido `draft` | evita confirmar sin semántica acordada | llamar `action_confirm` automáticamente |
| `client_order_ref` candidato | permite probar reconciliación | duplicar reliability dentro del connector |
| `httpx` existente como futuro transporte | no añade SDK grande | dependencia Odoo comunitaria no necesaria |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python3 -m unittest backend.tests.test_odoo_connector -v` | 18 tests OK |
| AST parse de `backend/app/**/*.py` | 156 archivos OK |
| `git diff --check` | OK en la revisión previa al cierre |
| `python3 -m unittest backend.tests.test_odoo_connector backend.tests.test_integration_registry backend.tests.test_sage200es_connector backend.tests.test_modular_order_hub backend.tests.test_order_delivery_reliability backend.tests.test_whatsapp_catalog_inbound backend.tests.test_templating_paths -v` | 86 tests OK |
| `python3 -m unittest backend.tests.test_schema_migrations backend.tests.test_core backend.tests.test_tenant_isolation backend.tests.test_static_assets -v` | 73 tests OK |
| `python3 -m unittest backend.tests.test_email_realtime_listener backend.tests.test_mail_inbox_routes backend.tests.test_email_ai_learning backend.tests.test_entries_inbox -v` | 39 tests OK; 1 error preexistente por `PermissionError` al escribir PDF |

## 10. Tests añadidos

La suite Odoo cubre registry, lazy import, secret boundary, JSON-2 request shape, ExternalIdentity de cliente/producto, variantes, pricing autoritativo, mapper determinista, draft semantics, faltantes, éxito, retry, permisos, validación, timeout/reconciliación, tenants Sage/Odoo, múltiples destinos y flujo WhatsApp → Odoo sin cambios en WhatsApp.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Odoo registrado | OK | `ConnectorDefinition("odoo")` |
| Lazy loading | OK | test en proceso limpio |
| Secret boundary | OK | raw `api_key` rechazado; reference separado |
| Customer mapping | OK | `ExternalIdentity` `res.partner` |
| Product variant mapping | OK | dos `product.product` sobre un template |
| Pricing trazable | OK | 10.00 → 8.70 |
| Reliability externa al connector | OK | `OrderDelivery` y retry existentes |
| Timeout/reconciliation | OK | un `ODOO-ORDER-000001`, sin duplicado |
| Tenant isolation | OK | Tenant A Sage / Tenant B Odoo |
| WhatsApp unchanged | OK | E2E usa el adapter existente |
| Workbench/Order/reliability sin cambios específicos Odoo | OK | no se modificaron esas capas |
| HTTP real bloqueado | OK | config y client gate |
| Migraciones | OK | ninguna |

## 12. Riesgos y observaciones

- Los fields de `sale.order`, `sale.order.line`, pricing e idempotencia deben verificarse por tenant en `/doc`.
- La API externa de Odoo depende del plan; el checklist exige confirmar Custom antes de contratar/configurar.
- API keys expiran y deben rotarse; el PoC solo conserva una referencia de secreto.
- Access rights, record rules y field access pueden variar por usuario bot.
- El payload de líneas es mock contract y no debe enviarse a una instancia real.

## 13. Desviaciones

Se añadió un pequeño gate genérico `mock_only` al registry/lifecycle para no duplicar excepciones específicas de Sage y permitir que Odoo sea seguro por defecto.

## 14. Estado final de Git

Bloque entregado en tres commits locales sobre `feat/modular-order-hub`. Worktree limpio, sin upstream, sin push y sin producción.

## 15. Recomendación para la siguiente fase

Discovery de una base Odoo cliente y contract verification contra `/doc`; solo después implementar `HTTPOdooJSON2Transport` y habilitar capabilities verificadas una por una.
