# Entrega de fase

## 1. Objetivo

Crear un `GenericRESTConnector` declarativo y seguro para integrar dos APIs REST con contratos diferentes mediante configuración, sin crear un adapter específico por cliente.

## 2. Alcance ejecutado

- Contrato DSL restringido para field paths, rename, arrays, constantes, optional/default y tipos básicos.
- `GenericRESTConnector → GenericRESTClient → MockGenericRESTTransport`.
- `HTTPGenericRESTTransport` preparado pero bloqueado.
- Capabilities derivadas de operations válidas por `IntegrationConnection`.
- ExternalIdentity namespace `generic_rest:<connection_id>`.
- AcmeERP A y ERP B con mappings distintos y sin clases específicas.
- Dry-run seguro y formulario mínimo de Settings.
- Límites, SSRF, headers, auth reference, status/retry mapping e idempotency header/body.
- Reconciliation delegada a `OrderDelivery`.
- Sin migraciones nuevas y sin cambios en WhatsApp, Order, Workbench, Sage, Odoo o reliability.

## 3. Alcance no ejecutado

- HTTP real, DNS resolution enforcement en un transport de producción y SecretProvider efectivo.
- Persistencia de auditoría de requests desde el worker; el PoC mantiene records seguros en memoria.
- XML, SOAP, CSV, multipart, GraphQL, marketplace de profiles y motor de paginación.
- Sincronización ERP → ANCHI de master data o inventario.

## 4. Diagnóstico previo

`IntegrationConnection.config_json` ya era un contenedor tenant-scoped y la tabla `ExternalIdentity` ya soportaba `external_system`. `OrderDelivery` ya resolvía una connection concreta, almacenaba attempts y delegaba retries/reconciliation. No hacía falta una tabla `generic_rest_connections`.

## 5. Cambios realizados

El registry registra `generic_rest` como experimental/mock-only sin capabilities globales. `normalize_and_validate_config()` aplica validación específica al guardar y activar. `capabilities_for()` deriva solo las operations válidas de la connection.

La DSL impide convertirse en un lenguaje de programación: no hay expresiones ni ejecución, los paths están allow-listed, los hosts solo proceden de `base_url` administrativa y los placeholders de path rechazan URLs absolutas. El transport mock demuestra tanto el happy path como timeout after create y errores retryable/permanentes.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/connectors/registry.py` | definition/factory lazy y capabilities dinámicas | registry |
| `backend/app/integrations/service.py` | validación JSON y DSL al guardar/activar | lifecycle |
| `backend/app/integrations/routes.py` | endpoint tenant-scoped de dry-run | API |
| `backend/app/templates/settings/integrations.html` | referencia SecretProvider y configuración no secreta | UI mínima |
| `backend/app/connectors/__init__.py` | exports lazy | wiring |
| `backend/tests/test_integration_registry.py` | expectativa de catálogo | regresión |
| `docs/architecture/integration-registry.md` | Generic REST en registry | arquitectura |
| `docs/architecture/modular-order-hub.md` | flujo y límites del bloque | arquitectura |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/connectors/generic_rest_mapping.py` | DSL, validación, SSRF/path/header policy y evaluator |
| `backend/app/connectors/generic_rest.py` | connector, client, transports mock/bloqueado y audit record seguro |
| `backend/tests/test_generic_rest_connector.py` | seguridad, mappings, Acme/Beta, dry-run, routing y reliability |
| `docs/architecture/generic-rest-connector.md` | contrato técnico |
| `docs/integrations/generic-rest-onboarding.md` | guía de configuración |
| `docs/development/block7-generic-rest-delivery.md` | acta de entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativa descartada |
|---|---|---|
| Configuración en `IntegrationConnection` | reutiliza lifecycle y tenant scope existentes | tabla específica por ERP |
| Capabilities por operation | evita anunciar soporte no configurado | capabilities globales de `generic_rest` |
| Namespace por connection | dos APIs genéricas no comparten identities | key global `generic_rest` |
| DSL allow-listed | suficiente para mappings comunes y auditable | eval, templates o scripting |
| Mock-only | permite probar end-to-end sin salida de red | activar HTTP prematuramente |
| `OrderDelivery` como reliability owner | una semántica común de retry/reconciliation | retry loop dentro del adapter |
| Header/body idempotency configurable | cubre APIs comunes sin inventar garantías | exactly-once universal |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `PYTHONPATH=backend python3 -m unittest backend.tests.test_generic_rest_connector backend.tests.test_integration_registry -v` | 35 tests OK |
| AST de módulos Generic REST y tests | OK |
| `git diff --check` | OK en la revisión final |
| suites Sage/Odoo/WhatsApp/reliability existentes | se ejecutan en la validación combinada final |

## 10. Tests añadidos o modificados

La suite cubre registry/lazy loading, configuración tenant-scoped, múltiples connections, capabilities derivadas, activación inválida, secret boundary, absolute URL/SSRF, ausencia de código arbitrario, DSL completa, response mapping, pricing/inventory opcionales, error mapping, idempotencia, timeout/reconciliation, Acme/Beta, dry-run sin HTTP, connection disabled, aislamiento tenant, WhatsApp structured zero-LLM y compatibilidad Sage/Odoo/reliability.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Dos APIs con schemas diferentes sin cambiar código | OK | fixtures Acme A/B y 2 configs |
| Connector declarativo sin scripting | OK | validator + tests de paths/code |
| No proxy SSRF | OK | localhost, metadata, private y absolute URL rechazados |
| Capabilities dinámicas | OK | conexión completa vs solo orders.create |
| ExternalIdentity aislada | OK | namespace por connection y tenant filters |
| Dry-run no ejecuta HTTP | OK | transport requests vacío |
| Idempotencia/reconciliation | OK | timeout after create sin duplicado |
| Reliability existente | OK | mismo `OrderDelivery`/attempts |
| WhatsApp/Sage/Odoo sin reimplementación | OK | regresiones y módulos intactos |
| HTTP real bloqueado | OK | `HTTPGenericRESTTransport` rechaza ejecución |
| Migraciones nuevas | OK | ninguna |

## 12. Riesgos y observaciones pendientes

- Antes de HTTP real se necesita resolución DNS/egress enforcement contra rebinding y un allowlist administrado.
- `private_network_allowlist` es una declaración de configuración del PoC; no sustituye controles de red.
- Debe conectarse la auditoría segura al worker sin persistir bodies ni secretos.
- Un destino sin idempotency/reconciliation no puede prometer effectively-once.

## 13. Desviaciones respecto al alcance inicial

La UI usa JSON estructurado validado en backend, no un visual mapper. Es deliberado: mantiene el onboarding pequeño sin permitir JSON arbitrario ni duplicar la DSL en frontend.

## 14. Estado final de Git

Cinco commits locales sobre `feat/modular-order-hub`; sin upstream, sin push y sin producción. No existe `PROJECT.md` en este worktree; se siguieron `AGENTS.md` y la plantilla de entrega.

## 15. Recomendación para la siguiente fase

Seleccionar un sandbox real de una API REST, completar SecretProvider y contract verification, y solo después implementar el transport HTTPS con egress allowlist, DNS seguro, métricas y auditoría persistente.
