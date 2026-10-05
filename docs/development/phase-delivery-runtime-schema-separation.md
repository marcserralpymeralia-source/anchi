# Entrega de fase — separación de migraciones y runtime

## 1. Objetivo

Eliminar DDL implícito de Web/Worker startup y onboarding runtime; dejar upgrades como comandos explícitos; hacer que schema ausente/stale falle cerrado y limitar pools runtime.

## 2. Alcance ejecutado

- `$diagnosing-bugs`: reproduje el DDL dentro del lifespan y worker provisioning, contrastándolo con el contrato de `docs/schema-migrations.md`.
- Separé validación read-only de schema y migraciones explícitas master/tenant.
- Añadí opciones bounded de pool para engines runtime y mantuve `pool_pre_ping`, incluido SQLite.
- Rehabilité la cola normal de provisioning como operación de datos sólo fuera de Catalog-only; espera en `retry/schema` cuando falta una migración y no crea archivos/tablas.
- Hice obligatorias las identidades migrator en `APP_ENV=production`, distintas del runtime y para el mismo destino; deshabilité `upgrade-all-tenants` en ese entorno.
- Actualicé contratos de migración, staging y gap register.

## 3. Alcance no ejecutado

No se accedió ni modificó DB, backup, secreto, Neon, Render, Blob, producción o nube. No se ejecutó SQL de grants, migraciones remotas, deploy, push o commit. Backup/restore one-shot quedó sin cambios.

## 4. Diagnóstico previo

El lifespan invocaba `init_master_db`, upgrade master y schema tenant, y también creaba/actualizaba datos y actores. El worker provisioning invocaba la misma vía DDL. El contrato histórico ya especificaba migrate → verify → start. El repro de lifespan falló deliberadamente al observar `init_master_db` antes del cambio.

La primera suite completa post-cambio reportó 735 tests, 11 failures y 9 errors. Ocho errores eran fixtures que dependían del DDL de onboarding/startup; dos fallos esperaban `pool_pre_ping` también en SQLite. Se ajustaron fixtures a migración explícita y se preservó pre-ping. Revisión independiente detectó además worker provisioning desconectado y falta de enforcement de identidad migrator; ambos quedaron corregidos.

## 5. Cambios realizados

- Startup valida master y tenants activos con informes `persist=False`; no crea schema ni datos. Tenants inactivos/en provisioning no bloquean startup mientras esperan migración.
- Web y Worker fallan cerrados si master o tenant activo está missing/stale/inaccesible; liveness sigue independiente.
- CLI admite URLs migrator por entorno, valida host/puerto/database y username distinto en production. Reports continúan read-only.
- Worker normal reintenta el provisionamiento data-plane sin DDL; Catalog-only conserva su exclusión.
- Los tests de onboarding ejecutan `upgrade_tenant_schema` antes de la fase de datos. No se modificó backup/restore.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/core/config.py` | Pool runtime acotado configurable | Configuración |
| `backend/app/core/lifespan.py` | Quitar migraciones/bootstrap/escrituras del startup | Runtime |
| `backend/app/core/schema_readiness.py` | Gate read-only y fail-closed | Nuevo módulo |
| `backend/app/db/database.py` | Pool bounded | Runtime DB |
| `backend/app/db/pool.py` | Opciones pool centralizadas | Nuevo módulo |
| `backend/app/external_databases/service.py` | Pool cliente bounded | Runtime DB |
| `backend/app/master/database.py` | Pool bounded | Runtime DB |
| `backend/app/master/provisioning.py` | Eliminar create/upgrade de schema en provisioning | Runtime |
| `backend/app/superadmin/service.py` | Inicializar filas sólo con schema current | Runtime |
| `backend/app/tenancy/database.py` | Pool bounded; engines runtime | Runtime DB |
| `backend/app/workers/jobs_worker.py` | Validación startup; preservar worker data-only normal | Worker |
| `backend/app/workers/provisioning_worker.py` | Retry seguro hasta migración, sin DDL | Worker |
| `backend/scripts/schema_migrations.py` | Credenciales migrator explícitas y guard production | CLI |
| `backend/tests/test_account_lifecycle.py` | Migración explícita en fixture | Test |
| `backend/tests/test_core.py` | Expectativa fail-closed / lifespan aislado | Test |
| `backend/tests/test_multitenancy_auth.py` | Preparar schema explícitamente | Test |
| `backend/tests/test_pilot_runtime_adaptations.py` | Adaptar fixture al gate read-only | Test |
| `backend/tests/test_static_assets.py` | Aislar readiness/worker para test de assets | Test |
| `backend/tests/test_superadmin.py` | Flujo async → migrate → data provisioning | Test |
| `backend/tests/test_runtime_schema_readiness.py` | No-DDL, fail-closed, retry, pools y migrator | Nuevo test |
| `docs/schema-migrations.md` | Corregir contrato, secuencia y credenciales | Documentación |
| `docs/pilot/pilot-staging-provisioning.md` | Añadir orden migrator/runtime | Documentación |
| `docs/pilot/pilot-gap-register.md` | Registrar cierre local y delta de suite | Documentación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/core/schema_readiness.py` | Schema gate sin escritura y errores sanitizados |
| `backend/app/db/pool.py` | Opciones reutilizables de pool runtime |
| `backend/tests/test_runtime_schema_readiness.py` | Cobertura de gate, worker, CLI y límites |
| `docs/development/phase-delivery-runtime-schema-separation.md` | Registro de entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Readiness consulta sólo compañías activas | Un tenant inactivo puede esperar migración operatoria sin tumbar Web; al activarse, el worker debe comprobar schema current | Bloquear todo servicio por tenant pendiente |
| Migrator distinto por username y mismo destino | Evita que staging/production ejecute DDL con identidad runtime | Fallback silencioso al runtime; aceptar destino arbitrario |
| Worker normal conserva data provisioning; Catalog-only lo excluye | Evita regresión de altas existentes sin reintroducir DDL | Eliminar procesamiento de cola o migrar desde Worker |
| SQLite conserva defaults de pool, con `pool_pre_ping` | Preserva dialect defaults y contrato previo | Forzar tamaño/overflow PostgreSQL en SQLite |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| Repro controlado de lifespan antes del fix | Rojo esperado: el startup llamaba la inicialización DDL |
| `PYTHONDONTWRITEBYTECODE=1 APP_ENV=test PYTHONPATH=backend python3 -m unittest backend.tests.test_runtime_schema_readiness backend.tests.test_superadmin backend.tests.test_account_lifecycle backend.tests.test_static_assets backend.tests.test_database_engines -q` | 26 tests, OK |
| `PYTHONDONTWRITEBYTECODE=1 APP_ENV=test PYTHONPATH=backend python3 -m unittest backend.tests.test_multitenancy_auth backend.tests.test_whatsapp_catalog_inbound backend.tests.test_order_extraction_schema backend.tests.test_order_delivery_reliability backend.tests.test_generic_rest_connector backend.tests.test_setup_onboarding backend.tests.test_jobs_reliability backend.tests.test_worker_tenant_scope backend.tests.test_security_config backend.tests.test_connector_security backend.tests.test_schema_migrations backend.tests.test_pilot_closure backend.tests.test_pilot_policy_enforcement backend.tests.test_pilot_configuration_manifest backend.tests.test_pilot_runtime_adaptations backend.tests.test_runtime_schema_readiness backend.tests.test_regression_flows.RegressionFlowsTests.test_mock_order_uses_tenant_company_name -q` | 238 tests, 0 failures, 0 errors |
| `PYTHONDONTWRITEBYTECODE=1 APP_ENV=test PYTHONPATH=backend python3 -m unittest discover -s backend/tests -p 'test_*.py' -q` | 738 tests, 8 failures, 1 error, 1 skipped; todos los resultados restantes coinciden con baseline/out-of-scope |
| AST parse de Python modificado/nuevo | 20 archivos, OK |
| `git diff --check` | OK |
| Secret scan additions (diff tracked + módulos nuevos) | Sin patrones de credenciales/secreto reales |
| Revisión estática de call sites `create_all`/upgrade | Sólo helpers de migración y tooling; no lifespan/Worker runtime |

## 10. Tests añadidos o modificados

Se comprueba schema current sin DDL SQL; master/tenant missing y stale fallan cerrados; SQLite inexistente no se crea; tenant pendiente permanece en retry hasta migración; Web/Worker no inician con schema stale; provisioning normal es data-only y Catalog-only no procesa esa cola; migrator requiere URL/identidad separada en production; destino y pool settings quedan acotados. Tests de superadmin/account migran fixtures explícitamente.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Web/Worker startup sin DDL | CERRADO LOCAL | Repro, SQL event tests y revisión de call sites |
| Missing/stale schema falla cerrado | CERRADO LOCAL | Tests de master, tenant, lifespan y Worker |
| Upgrades explícitos master/tenant | CERRADO LOCAL | CLI existente, overrides migrator con guard production |
| Pools runtime bounded y pre-ping | CERRADO LOCAL | Config, tests; fórmula 16 para Web + Worker, master + 1 tenant |
| Catalog-only crítica verde | CERRADO LOCAL | 238/238 tests |
| Full suite sin delta regresivo | CERRADO LOCAL | 738; fallos restantes son baseline/out-of-scope; el benchmark histórico cold/warm pasó en esta ejecución |
| Provisionamiento cloud / rehearsal | ABIERTO | No autorizado en esta tarea |

## 12. Riesgos y observaciones pendientes

- Validar roles/grants en Neon staging: runtime DML-only y migrator con ownership/DDL mínimo; el CLI no prueba membresía/privilegios efectivos del proveedor.
- Verificar conexión directa de migraciones/backup/restore y topología real en la branch vacía autorizada cuando se autorice provisioning.
- El full suite mantiene fallos históricos documentados: 3 Email autodiscovery; 3 G-26 Email; autoconfirmación WhatsApp fixture; audio/transcripción; error del test de eliminación de logs. No bloquean Catalog-only según perfil vigente.
- `PROJECT.md` no existe en el repo; se siguieron `AGENTS.md` y las instrucciones del worktree padre.

## 13. Desviaciones respecto al alcance inicial

Se adaptaron fixtures de superadmin/account/static-assets que dependían implícitamente del antiguo DDL. No se alteró su contrato funcional; el setup ahora representa explícitamente migrate → data provisioning. Los hallazgos iniciales de code review P1 se corrigieron dentro de esta fase.

## 14. Estado final de Git

Rama `feat/modular-order-hub`, HEAD base `8ca732b4339a08ef2c26eac95298c898af9f824a`; cambios locales sin commit. Sin push, deploy, cloud mutation ni producción.

## 15. Recomendación para la siguiente fase

Mantener `READY FOR PROVISIONING` / `NO GO PILOT`. Antes de staging, provisionar roles distintos y verificar los grants efectivos; luego ejecutar migrator, reportes `current`, runtime start, readiness y rehearsal en entorno aislado.
