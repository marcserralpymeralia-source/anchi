# Entrega de fase — BLOQUE 2 Reliability Layer

## 1. Objetivo

Preparar la entrega outbound de pedidos con outbox tenant-scoped, retries deterministas, idempotencia, reconciliación simulada y recuperación manual, sin HTTP ni conectores reales.

## 2. Alcance ejecutado

- `OrderDelivery` como outbox de dominio.
- `OrderDeliveryAttempt` como historial durable.
- Reutilización de `BackgroundJob`, `claim_next_job`, locks, dedupe, backoff y recuperación stale.
- Worker `deliver_order` separado del procesamiento inbound.
- FakeERP con escenarios de éxito, timeout, rate limit, indisponibilidad, validación permanente y health configurable.
- Reconciliación por idempotency key.
- Retry manual y aislamiento por tenant.

## 3. Alcance no ejecutado

- HTTP, Meta, Sage, SAP, Odoo u otro ERP real.
- UI de DLQ.
- Circuit breaker y bulkheads; quedan para una fase posterior si un connector real lo requiere.

## 4. Diagnóstico previo

Se conservaron `BackgroundJob` y el worker existente. `ExportJob` no se reutilizó como outbox porque representa archivos/FTP y no una intención de entrega connector-neutral.

## 5. Cambios realizados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/db/models.py` | Outbox e intentos | Modificado |
| `backend/app/migrations/registry.py` | Migración `2026.09.30.2` | Modificado |
| `backend/app/connectors/contracts.py` | Errores, health y reconciliación | Modificado |
| `backend/app/connectors/fake.py` | Escenarios deterministas | Modificado |
| `backend/app/orders/delivery.py` | Servicio de entrega y retry manual | Nuevo |
| `backend/app/orders/modular.py` | Validated → outbox | Modificado |
| `backend/app/workers/jobs_worker.py` | Worker `deliver_order` | Modificado |
| `docs/architecture/modular-order-hub.md` | Lifecycle y garantías | Modificado |

## 6. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/orders/delivery.py` | Orquestación outbox/delivery/retry/reconciliation |
| `backend/tests/test_order_delivery_reliability.py` | Escenarios reliability y concurrencia |

## 7. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Mantener `BackgroundJob` | Ya aporta dedupe, claim, lock y backoff | Worker paralelo |
| Crear `OrderDelivery` | `ExportJob` tiene semántica FTP/archivo | Forzar `ExportJob` como outbox |
| Idempotency key en DB | Protección frente a concurrencia | Comprobación solo Python |
| Backoff sin jitter | Determinismo y tests sin sleeps | Jitter no controlado |
| Reconciliación opcional del contrato | ERPs futuros pueden exponer búsqueda distinta | Afirmar exactly-once universal |

## 8. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python -m compileall -q backend/app backend/tests` | OK |
| `python -m unittest tests.test_order_delivery_reliability tests.test_modular_order_hub` | 20 tests OK |
| `python -m unittest tests.test_jobs_reliability tests.test_export_service tests.test_worker_tenant_scope` | 27 tests OK |
| Suites email, WhatsApp y Workbench focalizadas | 92 tests OK |
| `git diff --check` | OK |

## 9. Tests añadidos o modificados

Se cubren outbox único, dedupe, success, external ID, timeout retryable, backoff, error permanente, máximo de intentos, retry manual, remote success + local timeout, health, rate limit, concurrencia, tenant isolation y aislamiento entre connectors.

## 10. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Un pedido validado genera un outbox/job | Cumplido | Suite reliability |
| No duplicar delivery ni pedido remoto | Cumplido | Unicidad DB + reconciliación FakeERP |
| Retry/DLQ/manual retry | Cumplido | Estados y tests deterministas |
| Worker tenant-scoped | Cumplido | Claim y test de aislamiento |
| Regresiones existentes | Cumplido | 27 + 92 tests OK |

## 11. Riesgos pendientes

La garantía es effectively-once/idempotente para destinos con referencia consultable; no se afirma exactly-once para destinos sin idempotencia o reconciliación. Circuit breaker y UI de DLQ quedan pendientes.

## 12. Estado final de Git

Rama `feat/modular-order-hub`, sin upstream, sin push. Los commits se mantienen locales y pequeños.

## 13. Recomendación para la siguiente fase

No iniciar BLOQUE 3 hasta observar esta capa con un connector sandbox. El siguiente candidato es `WhatsApp Catalog structured inbound`, reutilizando el mismo contrato inbound y la salida reliability ya estabilizada.
