# Entrega de fase — BLOQUE 4B

## 1. Objetivo

Construir el primer adapter ERP de ANCHI como PoC local: `Sage200ESConnector` con transporte mock/fixtures, sin llamadas HTTP reales ni contrato Sage inventado.

## 2. Alcance ejecutado

- Connector explícito `sage_200_es`.
- Configuración tenant-scoped con site y gate `real_http_enabled=False`.
- Separación `Sage200ESConnector → Sage200ESClient → Transport`.
- `MockSage200ESTransport` para sites, customers, products, rates, price offers, pedidos, errores y reconciliación.
- Mapping puro `Sage200ESSalesOrderMapper`.
- ExternalIdentity obligatoria para clientes y productos Sage.
- Pricing separado entre `source_price` y `authoritative_price`.
- Idempotencia y reconciliación reutilizando `OrderDelivery`.
- Fixtures sintéticos marcados `UNVERIFIED_SAGE_CONTRACT`.
- Flujo end-to-end WhatsApp Catalog → Order → Delivery → Sage mock → `SAGE-ORDER-000001`.
- Informe de verificación contractual y actualización de arquitectura/plan.

## 3. Alcance no ejecutado

- No existe `HTTPSage200ESTransport`.
- No se ejecutaron llamadas contra Sage.
- No se habilitó ninguna capability real Sage.
- No se añadieron migraciones ni campos `sage_*`.
- No se modificaron WhatsApp, Workbench ni la reliability layer.
- No se implementó BLOQUE 4C.

## 4. Diagnóstico previo

La documentación pública de Sage 200 España confirma Soluciones Conectadas y sus prerrequisitos, pero no un contrato completo de endpoints, payloads, idempotencia o errores. El PoC mantiene esas operaciones como mock-only y deja las capabilities reales vacías.

## 5. Cambios realizados

Se añadió el adapter local y su suite de pruebas. El registro de connector se exporta desde `app.connectors`, pero se inyecta explícitamente en el flujo de prueba; el registry por defecto de FakeERP no se altera.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/connectors/__init__.py` | Exponer las clases del PoC | Código |
| `docs/architecture/modular-order-hub.md` | Documentar integración mock y gates | Documentación |
| `docs/development/block4b-sage-poc-plan.md` | Registrar resultados A–M | Documentación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/connectors/sage200es.py` | Connector, client, config, mapper y transport mock |
| `backend/tests/fixtures/sage200es_mock_contract.json` | Datos sintéticos provisionales |
| `backend/tests/test_sage200es_connector.py` | 26 pruebas del PoC |
| `docs/integrations/sage200es-contract-verification.md` | Matriz de operaciones verificadas/mock/runtime |
| `docs/development/block4b-sage-poc-delivery.md` | Registro de entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| `Sage200ESConnector` explícito | Evita ocultar producto/país/edición | `SageConnector` genérico |
| `capabilities = frozenset()` | Ninguna operación detallada está verificada oficialmente | Activar capabilities por analogía |
| `mock_capabilities` separado | Permite probar el flujo sin afirmar soporte real | Mezclar mock y runtime |
| ExternalIdentity obligatoria | Evita resolver outbound por teléfono, nombre o matching semántico | Inventar IDs Sage |
| Mapper puro | Aísla DTO conceptual de HTTP/retries/persistencia | Lógica Sage en Order/WhatsApp |
| Pricing dual | No confunde precio Meta con autoridad ERP | Pricing engine dentro de ANCHI |
| Outbox/reconciliation existente | BLOQUE 2 ya posee la política de delivery | Retry paralelo dentro del connector |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `rtk python3 -m unittest backend.tests.test_sage200es_connector` | 26/26 OK |
| `rtk python3 -c ... ast.parse + json.loads` | AST y JSON OK |
| `rtk git diff --check` | OK |
| Suite modular/reliability/WhatsApp/Workbench/regression | 60 OK; 1 error baseline ajeno: escritura de PDF en `backend/app/storage` no permitida por el worktree |

## 10. Tests añadidos o modificados

Añadida la suite `test_sage200es_connector.py`; no se modificaron tests existentes.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Connector identifier `sage_200_es` | Cumplido | Suite específica |
| Configuración tenant/site aislada | Cumplido | `Sage200ESConfig` y tests |
| Product/customer ExternalIdentity | Cumplido | Mapper y tests de ausencia |
| Source vs authoritative price | Cumplido | `Sage200ESPriceResolution` |
| Mapping determinista | Cumplido | `Sage200ESSalesOrderMapper` |
| 429/503/timeout/validation | Cumplido | Mock transport y delivery tests |
| Remote create + timeout + reconciliation | Cumplido | `SAGE-ORDER-000001` sin duplicado |
| WhatsApp sin LLM | Cumplido | Flujo estructurado y patch de test |
| No HTTP real | Cumplido | Gate de configuración y ausencia de transport HTTP |
| No migración | Cumplido | Reutiliza modelo existente |

## 12. Riesgos y observaciones pendientes

- La suite de regresión tiene un error previo de permisos al escribir el PDF mock; no se corrigió por estar fuera de alcance.
- Sage 200 ES aún no tiene contrato público detallado verificado para runtime.
- `orders.create`, rates, price offers y reconciliación son mock-only.
- El registry de producción no debe registrar el connector hasta definir configuración segura por tenant.

## 13. Desviaciones respecto al alcance inicial

Se añadió este documento de entrega para cumplir la plantilla del repositorio. No se implementó HTTP real ni se alteró la infraestructura.

## 14. Estado final de Git

Se crearán commits pequeños de código, tests y documentación. No se hará push; el worktree seguirá sin upstream.

## 15. Recomendación para la siguiente fase

BLOQUE 4C debe validar el contrato contra una instancia Sage 200 real/controlada con producto, versión, Advanced, Soluciones Conectadas, site, credenciales/API y autorización explícita. Solo después se podrá diseñar `HTTPSage200ESTransport`.

