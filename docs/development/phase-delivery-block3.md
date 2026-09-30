# Entrega de fase — BLOQUE 3 WhatsApp Catalog Structured Inbound

## 1. Objetivo

Incorporar WhatsApp Catalog como primera entrada estructurada real, reutilizando el webhook, la persistencia y el outbox existentes, con FakeERP como único destino local.

## 2. Alcance ejecutado

- Adaptador `WhatsAppCatalogInboundConnector` para `type=order`.
- Resolución determinista de cliente por `meta_whatsapp` y fallback telefónico.
- Resolución determinista de producto por `meta_whatsapp_catalog` y `product_retailer_id`.
- Autorización tenant-scoped por `catalog_id` y configuración local.
- Preservación de source price, currency, comentario y catálogo.
- Dedupe por tenant/provider/wamid y reutilización de `OrderDelivery`.
- Validación controlada de cantidades, monedas y mensajes inválidos.
- Límite de 512 KiB para payloads de webhook.

## 3. Alcance no ejecutado

- No hay ERP real, Graph API, outbound Catalog, confirmación ni respuestas automáticas.
- No hay sincronización ERP → Meta Catalog.
- No hay UI de mapping ni motor de pricing/FX.

## 4. Diagnostico previo

El webhook Meta existente ya resolvía tenant, verificaba token/firma, persistía `InboundMessage`, deduplicaba por provider/external ID y encolaba jobs. El flujo conversacional existente debía mantenerse intacto para tipos no `order`.

## 5. Cambios realizados

- Nuevo contrato enriquecido con sistema de identidad de cliente, sistema de producto y catálogo de origen.
- Nuevo adaptador y processor Catalog antes de instanciar el pipeline de agente.
- Configuración `catalog_enabled`, `catalog_orders_enabled`, `catalog_id`.
- Migración incremental `2026.09.30.3` para `orders.source_catalog_id`.
- Fixtures sanitizados A–J cubiertos por pruebas unitarias y de worker.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/connectors/contracts.py` | Identidades de cliente/producto y catálogo | Contrato |
| `backend/app/connectors/__init__.py` | Exposición del adaptador | Registro |
| `backend/app/db/models.py` | Persistencia del catálogo de origen | Modelo |
| `backend/app/migrations/registry.py` | Migración posterior a `.2` | Esquema |
| `backend/app/orders/modular.py` | Resolución determinista y fallback telefónico | Core |
| `backend/app/whatsapp/routes.py` | Clasificación, autorización y límite de payload | Webhook |
| `backend/app/whatsapp/service.py` | Configuración, parsing y comentarios | WhatsApp |
| `backend/app/workers/jobs_worker.py` | Desvío Catalog sin agente | Worker |
| `backend/tests/test_modular_order_hub.py` | Cobertura de migración `.3` | Tests |
| `docs/architecture/modular-order-hub.md` | Arquitectura y mapping | Documentación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/connectors/whatsapp_catalog.py` | Adaptación Meta → contrato structured |
| `backend/app/whatsapp/catalog.py` | Bindings y procesamiento inbound Catalog |
| `backend/tests/test_whatsapp_catalog_inbound.py` | Fixtures y pruebas A–J/end-to-end local |
| `docs/development/phase-delivery-block3.md` | Registro de entrega |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| Reutilizar webhook Meta | Conserva verificación, tenant routing, inbox y dedupe | Endpoint paralelo |
| `meta_whatsapp_catalog` como sistema de producto | Identidad estable del retailer ID | Matching semántico o alias heurístico |
| `source_catalog_id` en `Order` | Auditoría y autorización sin jerarquía nueva | Tabla de catálogos compleja |
| Source price en `OrderLine` | No confundirlo con precio autoritativo | FX o consulta ERP |
| Review ante datos no resolubles | Evita exportaciones inventadas | Fuzzy/LLM/autocorrección |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 TEMP_STORAGE_DIR=/private/tmp/anchi-modular-test rtk proxy python3 -m unittest tests.test_whatsapp_catalog_inbound -v` | 10 OK |
| `... unittest tests.test_modular_order_hub tests.test_order_delivery_reliability ...` | 85 OK |
| `rtk git diff --check` | OK |
| `python -m compileall` | Bloqueado por permisos de escritura de `__pycache__`; se revalidó la ejecución de imports/tests sin bytecode |

## 10. Tests añadidos o modificados

Se cubren single/multi-item, comentario, wa_id, fallback telefónico, retailer IDs, catálogo autorizado/no autorizado, duplicación de wamid, batch text/order, producto/cliente desconocidos, cantidad inválida, moneda mezclada y ausencia de AgentProcessingService.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| Meta fixture → Order | OK | Suite Catalog |
| Order → OrderDelivery → FakeERP | OK | Worker y reliability |
| Sin LLM/semantic/embedding | OK | Mock de `AgentProcessingService` |
| Dedupe inbound/outbox | OK | Test de wamid duplicado |
| Tenant isolation | OK | Identidades/configuración filtradas por `company_id` |
| WhatsApp existente | OK | Suite de integración existente |
| Workbench y jobs/reliability | OK | Suites heredadas focalizadas |

## 12. Riesgos y observaciones pendientes

- El mapping de identidades todavía se mantiene por helpers/seed; no existe UI operativa.
- FakeERP acepta el source price; un ERP real debe definir el precio autoritativo y la política de discrepancias.
- La suite completa tiene un baseline conocido de duración; no se ejecutó completa en este bloque.

## 13. Desviaciones respecto al alcance inicial

Se añadió un límite explícito de 512 KiB porque la ruta solo registraba el tamaño del cuerpo y no lo rechazaba. No se introdujeron llamadas externas ni configuración real de Meta.

## 14. Estado final de Git

Cambios locales sin push. Los commits deben mantenerse pequeños y separados entre adapter/core, tests y documentación, según la instrucción de la fase.

## 15. Recomendacion para la siguiente fase

BLOQUE 4 debe seleccionar una variante concreta de Sage —producto, edición, región, API y modalidad de integración— antes de implementar el primer connector ERP real. No iniciar un connector “Sage” genérico.
