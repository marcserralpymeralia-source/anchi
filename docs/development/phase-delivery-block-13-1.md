# Entrega de fase — Block 13.1

## 1. Objetivo

Aplicar en backend la matriz RBAC aprobada, guards de confirmación, reglas de retención y detección de configuration drift para Catalog-only.

## 2. Alcance ejecutado

Controles tenant/platform, validación de pedidos, servicio de retención, fecha persistente de offboarding, manifiesto/hash, drift en readiness, pruebas y documentación.

## 3. Alcance no ejecutado

Sin push, deploy, producción, credenciales, infraestructura cloud, cleanup de datos reales, browser final ni conectores reales Sage/Odoo/HTTP.

## 4. Diagnóstico previo

HEAD limpio `71023850f891bc94dd90611946cf756f4e8b9697`, rama `feat/modular-order-hub`. Administrador heredaba permisos de logs técnicos y plataforma; el AuditLog tenant tenía endpoint destructivo; confirmación dependía en parte de toggles de scoring; faltaba ancla durable de offboarding.

## 5. Cambios realizados

RBAC de tenant separado de plataforma; AuditLog tenant no borrable; confirmación exige revisión y datos válidos; RetentionPolicyService tenant-scoped con dry-run/legal hold/gates; columna `offboarded_at` y migración aditiva; manifiesto hash y drift de readiness.

## 6. Archivos modificados

| Archivo | Motivo | Tipo |
|---|---|---|
| `backend/app/auth/dependencies.py` | Separar contexto platform/tenant | RBAC |
| `backend/app/core/permissions.py` | Alinear permisos y rutas | RBAC |
| `backend/app/core/retention.py` | Evaluar y aplicar retención de forma segura | Servicio nuevo |
| `backend/app/core/pilot_configuration.py` | Manifest, redacción, hash y comparación | Servicio nuevo |
| `backend/app/health/routes.py` | Exponer drift con datos del tenant activo | Readiness |
| `backend/app/logs/routes.py` | Preservar AuditLog tenant | RBAC/retención |
| `backend/app/master/models.py` | Campo `offboarded_at` | Modelo |
| `backend/app/master/service.py` | Contexto Superadmin platform-only | RBAC |
| `backend/app/migrations/registry.py` | Migración `2026.10.02.1` | Migración |
| `backend/app/orders/service.py` | Hard guards al confirmar | Validación |
| `backend/app/superadmin/service.py` | Estampar fecha de offboarding | Retención |
| `backend/tests/test_pilot_closure.py` | Probar el ancla | Test |
| `backend/tests/test_schema_migrations.py` | Migración aditiva/idempotente | Test |
| `backend/tests/test_pilot_policy_enforcement.py` | Matriz, confirmación, retención | Test nuevo |
| `backend/tests/test_pilot_configuration_manifest.py` | Hash, redacción, drift, tenant | Test nuevo |
| `docs/pilot/pilot-gap-register.md` | Reconciliar estado de gates | Documentación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/pilot/block-13-1-policy-enforcement-configuration-baseline.md` | Informe de bloque |
| `docs/development/phase-delivery-block-13-1.md` | Esta entrega basada en plantilla |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativa descartada |
|---|---|---|
| Persistir `offboarded_at` | Ancla durable requerida para +90 días | Inferir desde texto de AuditLog |
| Denegar borrado de AuditLog tenant | Preservar trazabilidad | Borrado masivo del Administrador |
| Borrado de negocio con gate + allowlist | Safe default | Activación implícita |
| Baseline JSON opt-in | Exigir aprobación explícita | Autoaprobar runtime o autocorregir drift |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `rtk env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest backend.tests.test_pilot_policy_enforcement backend.tests.test_pilot_configuration_manifest backend.tests.test_pilot_closure backend.tests.test_schema_migrations backend.tests.test_security_config backend.tests.test_order_delivery_reliability` | 82 tests, OK |
| Pilot-critical Catalog-only (suite Block 13 más policy/config y sólo regresión mock de Catalog) | 205 tests, 0 failures, 0 errors, OK |
| Ejecución ampliada con toda `test_regression_flows` | 217 tests, 3 failures: confirm/retry de Email fuera del perfil; reproducidos individualmente y registrados G-26 |
| Los 4 fallos opcionales conocidos ejecutados por separado | FAIL esperado: 3 email autodiscovery + 1 audio/transcripción; exactamente las exclusiones documentadas |
| AST de 15 archivos Python | OK, sin crear bytecode |
| `rtk git diff --check` | OK |

## 10. Tests añadidos o modificados

Roles allow/deny, retry, contexto plataforma, confirm blockers, tenant scoping, expiraciones, holds, dry-run, gate destructivo, migración, hash estable, redacción y clasificación de drift.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| RBAC local alineado | PASS local | Test de roles |
| Confirmación no bypassable por operador | PASS local | Test de guards |
| Retención tenant-scoped y safe default | PASS local | Tests servicio/migración |
| Drift/hash/redacción | PASS local | Tests manifest |
| Suite crítica Catalog-only | PASS | 205 tests, 0 failures/errors; 3 regresiones email excluidas y rastreadas en G-26 |
| Aislamiento de 4 fallos opcionales | PASS de clasificación | Los 4 fallan aislados tal como están conocidos y permanecen excluidos |
| Runtime final aprobado | OPEN | No disponible/autorizado |
| GO PILOT | NO GO | Gaps P1 externos abiertos |

Comando pilot-critical ejecutado (mantiene la prueba de regresión mock Catalog, excluye el flujo Email fuera del perfil):

```bash
rtk env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest \
  backend.tests.test_multitenancy_auth \
  backend.tests.test_whatsapp_catalog_inbound \
  backend.tests.test_order_extraction_schema \
  backend.tests.test_order_delivery_reliability \
  backend.tests.test_generic_rest_connector \
  backend.tests.test_setup_onboarding \
  backend.tests.test_jobs_reliability \
  backend.tests.test_worker_tenant_scope \
  backend.tests.test_security_config \
  backend.tests.test_connector_security \
  backend.tests.test_schema_migrations \
  backend.tests.test_pilot_closure \
  backend.tests.test_pilot_policy_enforcement \
  backend.tests.test_pilot_configuration_manifest \
  backend.tests.test_regression_flows.RegressionFlowsTests.test_mock_order_uses_tenant_company_name
```

Los tests residuales se ejecutaron con sus nombres exactos: tres métodos de `EmailAutoconfigTests` y `WhatsAppIntegrationTests.test_audio_download_waits_for_transcription_before_pipeline`; FAIL esperado para los cuatro casos excluidos.

## 12. Riesgos y observaciones pendientes

Retention requiere un caller/scheduler y cleanup configurados por operador. Runtime necesita migrar master, baseline aprobado, storage durable, cron, alertas, Postgres/restore y browser acceptance. Secret revocation y backup rotation son responsabilidad del proveedor.

## 13. Desviaciones respecto al alcance inicial

Se añadió sólo `offboarded_at` como migración aditiva necesaria para el ancla de retención; no se añadieron tablas ni borrado automático de negocio.

## 14. Estado final de Git

Pendiente de revisión final del diff; sin commit ni push.

## 15. Recomendación para la siguiente fase

En runtime aislado, aprobar/configurar baseline, ejecutar migración master, integrar cleanup técnico con scheduler controlado y cerrar persistencia/alertas/browser antes de reconsiderar el gate. No activar borrado de negocio sin aprobación operacional específica.
