# Entrega de fase — Block 13.2

## 1. Objetivo

Adaptar localmente ANCHI al runtime piloto aislado Render Web + Render Background Worker + Render Cron + Neon PostgreSQL + Vercel Blob privado y habilitar Generic REST real con controles fail-closed, sin aprovisionar ni desplegar.

## 2. Alcance ejecutado

Selector explícito de backend de adjuntos; probe de readiness; profile Catalog-only para jobs y cron; invocador de cron HTTP; transporte Generic REST con SecretProvider/egress; restauración PostgreSQL validada; tests y documentación del provisioning.

## 3. Alcance no ejecutado

No se crearon recursos, ramas Neon, stores, secretos ni servicios; no hubo deploy, push, producción, tráfico HTTP real ni uso de datos reales. No se ejecutaron Browser E2E ni pruebas contra proveedores externos.

## 4. Diagnóstico previo

Se trabajó en `feat/modular-order-hub`, HEAD `1e357a2e0c762a02be51c68e9504d54c268dbebf`. El único cambio previo era el documento sin seguimiento `docs/pilot/pilot-staging-provisioning.md`; se preservó y actualizó según solicitud. `PROJECT.md` no existe en este worktree. No se creó commit.

## 5. Cambios realizados

- Storage se selecciona por configuración explícita. Blob usa token explícito, URLs privadas validadas, namespace tenant-scoped y probe acotado; errores no hacen fallback a filesystem.
- Si Blob devuelve una URL de otro store, el objeto recién creado se borra best-effort antes de rechazarlo; aplica a adjuntos y probe de readiness.
- Readiness separa liveness, reporta perfil/gates sanitizados y bloquea Catalog-only cuando storage durable o baseline/configuración requerida no están listos.
- Worker filtra jobs a Catalog-only, omite trabajos de módulos excluidos y termina ordenadamente ante SIGTERM/SIGINT.
- Cron staging invoca `/cron/jobs` protegido, sin seguir redirects ni imprimir secretos; rutas de Email y snapshots quedan deshabilitadas para Catalog-only.
- Generic REST real se instancia sólo con perfil/gates/credencial SecretProvider/host HTTPS permitido e idempotencia. No cae silenciosamente al mock.
- Restore PostgreSQL valida metadata, checksum, identidad/base destino distinta, transacción y conectividad final.
- Se actualizó el plan de provisioning, profile, gap register y runbook de restore.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/connectors/generic_rest.py` | Transporte seguro, clasificación y health fail-closed sin probe | Integración |
| `backend/app/connectors/generic_rest_mapping.py` | Exigir idempotencia para `orders.create` real | Política |
| `backend/app/connectors/registry.py` | Selección real fail-closed | Integración |
| `backend/app/core/attachment_storage.py` | Selección configurada, token y validación de URL | Storage |
| `backend/app/core/backup.py` | Restore PostgreSQL con integridad/identidad | Backup |
| `backend/app/channels/routes.py`, `backend/app/orders/routes.py`, `backend/app/workbench/routes.py`, `backend/app/whatsapp/service.py`, `backend/app/settings/integrations.py` | Exigir tenant al leer adjuntos; bloquear Email reprocess Catalog-only | Storage/política |
| `backend/app/core/pilot_configuration.py` | Gates, hosts allowlisted, backend en manifest y estado de perfil común | Configuración |
| `backend/app/core/storage.py` | Selector y probe Blob | Storage |
| `backend/app/cron/routes.py` | Aislar cron fuera del perfil; bloqueos cron producen 503 | Cron |
| `backend/app/core/lifespan.py` | Forzar off de worker Email/job interno en Catalog-only | Startup |
| `backend/app/health/routes.py` | Liveness/readiness y gates | Health |
| `backend/app/integrations/onboarding.py` | No declarar éxito sin probe remoto Generic REST | Onboarding |
| `backend/app/integrations/readiness.py` | Validar gates/SecretProvider y respetar el resultado del test más reciente | Readiness |
| `backend/app/integrations/service.py` | Configurar Generic REST real sin persistir secreto | Integración |
| `backend/app/workers/jobs_worker.py` | Allowlist Catalog-only fail-closed y shutdown | Worker |
| `backend/app/whatsapp/routes.py` | Media excluida queda revisable sin job huérfano en ambas rutas webhook | Ingress |
| `backend/scripts/pilot_backup_restore.py` | CLI de restore PostgreSQL | Operación |
| `backend/tests/test_generic_rest_connector.py` | No fallback, idempotencia y health real no verificado sin probe | Tests |
| `backend/tests/test_pilot_configuration_manifest.py` | Gates/configuración segura | Tests |
| `backend/tests/test_import_storage.py`, `backend/tests/test_pilot_closure.py` | Selección Blob/lectura tenant storage | Tests |
| `backend/tests/test_pilot_runtime_adaptations.py`, `backend/tests/test_postgres_backup_restore_cli.py` | Gates fail-closed, cron health y base destino vacía | Tests |
| `docs/pilot/catalog-only-pilot-profile.md` | Alinear gates de runtime | Documentación |
| `docs/pilot/pilot-backup-restore-runbook.md` | Restore seguro y configuración de informes | Runbook |
| `docs/pilot/pilot-gap-register.md` | Estado Block 13.2 | Registro |
| `docs/pilot/pilot-staging-provisioning.md` | Plan actualizado de staging | Plan preexistente actualizado |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/scripts/pilot_cron_invoker.py` | Invocación puntual, autenticada y segura del endpoint cron |
| `backend/tests/test_pilot_runtime_adaptations.py` | Tests de storage, cron, readiness y worker |
| `backend/tests/test_postgres_backup_restore_cli.py` | Tests de integridad y restore PostgreSQL |
| `docs/development/phase-delivery-block-13-2.md` | Registro de entrega |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| Selección Blob explícita por configuración | Render no expone la detección de runtime Vercel | Detección implícita; fallback temporal |
| Namespace Blob privado por tenant | Compartir store no debe compartir objetos lógicamente | Store público o namespace global |
| Hostname Blob exacto fijado por `BLOB_STORE_HOST` | No exponer token a otra URL de Blob privada | Confiar sólo en sufijo de dominio |
| Ambos perfiles Catalog-only deben coincidir | Evita allowlist parcial; configuración no local ausente/inconsistente falla cerrada | Habilitar jobs por un único flag |
| Render Cron llama el endpoint protegido existente | Reutiliza auth/idempotencia/locking actuales | Scheduler nuevo dentro del proceso web |
| Generic REST usa SecureHTTPTransport y SecretProvider existente | Mantiene allowlist/secretos fuera de DB | HTTP directo o mock como fallback |
| Restore exige identidad distinta y destino vacío | Evita restauración sobre origen o datos preexistentes | `--clean`/sobrescritura |
| Generic REST real queda BLOCKED sin probe remoto seguro | Evita éxito ficticio; el contrato actual no define operación de health | Reusar el estado saludable del mock |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| Suites focalizadas de Block 13.2 | 63 tests, OK, 0 failures, 0 errors; tras corregir cleanup de Blob, los 3 tests directos del ciclo de vida también pasan |
| Selección pilot-critical Catalog-only ampliada con adaptaciones/restore (posterior al fix) | 229 tests, OK, 0 failures, 0 errors |
| `rtk env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest discover -s backend/tests -p 'test*.py'` | Suite final post-atribución: 720 tests, 9 failures, 1 error, 1 skipped. Los 3 fallos y 1 error fuera del baseline/G-26 están identificados test-a-test; el fallo adicional del benchmark es aislamiento frío/caliente, no una regresión 13.2. |
| Delta full-suite test-a-test frente al baseline registrado (681 tests, 4 failures, 1 skipped) y G-26 | Los cuatro fallos baseline siguen siendo los mismos: 3 Email autodiscovery y 1 audio/transcripción. También persisten los 3 tests Email documentados en G-26. `MessagesAndConversationsTests.test_safe_whatsapp_auto_confirms_without_pending_review` es preexistente: el fixture tiene `lines=[]` y no asigna cliente validado, mientras `validate_confirmation()` exige ambos; devuelve `auto_confirmed=False` y abre review. `ObservabilityTests.test_log_download_and_delete_are_scoped_to_company` es preexistente: el test pasa `db=` a `delete_logs`, cuya firma en HEAD sólo acepta `user` y cuyo contrato deniega borrar AuditLog. `OrdersDetailOptimizationTests.test_order_detail_query_budget_large` se clasifica `TEST_ISOLATION`: el mismo fixture da 16 queries en la primera petición fría y 15 en la segunda; una variante anterior también calienta el proceso. El diff 13.2 de `orders/routes.py` no toca el GET medido; tests y ruta compartida son preexistentes. |
| `$code-review` Standards + Spec | Revisión manual de ambos ejes. No se pudieron iniciar subagentes por límite de hilos; se informó la limitación. Revisados diff tracked y untracked. Finding P2 de cleanup ante host Blob inesperado corregido y cubierto por test. |
| `rtk git diff --check` | OK |
| Parseo AST sin bytecode | OK, 250 archivos Python |
| Browser, cron, storage, DB y alertas del proveedor | OPEN; sin runtime ni autorización externa |

## 10. Tests añadidos o modificados

Selección de backend y ausencia de fallback, lectura Blob tenant-scoped, throttling/sanitización del probe, cron invoker, allowlist Catalog-only también por PILOT_PROFILE, shutdown worker, readiness 503/liveness y outcome del test, Generic REST fail-closed y restore PostgreSQL con checksum/identidad/destino vacío.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| Adaptación local Render/Neon/Blob | PASS local | Código y suite focalizada |
| Perfil Catalog-only en jobs/cron | PASS local | Tests de allowlist y rutas |
| Generic REST real sin fallback ni falso health-success | PASS local; probe operativo OPEN | Tests y gates de configuración |
| Restore PostgreSQL seguro | PASS sintético | CLI y tests unitarios de destino vacío |
| Suite pilot-critical | PASS | 229 tests, 0 failures/errors |
| Runtime de proveedor, storage durable, cron/alertas, browser | OPEN | No hay ambiente final validado |
| Provisioning | No ejecutado | Requiere autorización/recursos |
| GO PILOT | NO GO | Gates externos siguen abiertos |

Los dos resultados inicialmente no atribuidos a baseline/G-26 quedaron clasificados como **PREEXISTING**. La suite final descubrió además el benchmark cold/warm, clasificado **TEST_ISOLATION** y documentado como fuera del scope 13.2. No se modificó código ni tests ajenos a la fase. La suite pilot-critical final terminó con **229 tests, 0 failures, 0 errors**; la suite completa no está totalmente verde, pero todos sus resultados se compararon y atribuyeron test-a-test sin una regresión de Block 13.2. El benchmark de rendimiento no constituye un gate funcional Catalog-only.

## 12. Riesgos y observaciones pendientes

Validar en entorno aislado: branch/database Neon vacía; hostname/token Blob privado exacto compartido por Web/Worker; write-restart-read; ambos perfiles explícitos e iguales; servicios separados; cron real e idempotencia; alertas; backup/restore y reinicios; browser critical path; synthetic pilot y recuperación de entrega. Resolver antes de considerar Generic REST conectado: probe remoto seguro configurado o aceptación de una operación sintética autorizada; el test actual se mantiene BLOCKED sin emitir HTTP. Resolver también host/sandbox, alcance/rotación del token Blob, límites de red, alertas/operadores y baseline aprobada. En runtimes no locales sin perfil válido, los jobs y workers internos quedan deshabilitados; el operador deberá configurar explícitamente el perfil aprobado. No se acredita aislamiento cloud ni operación real con tests locales.

## 13. Desviaciones respecto al alcance inicial

No se añadieron conectores ni scheduler nuevo. Se añadió un pequeño CLI para llamar el endpoint HTTP existente y un CLI seguro para restore PostgreSQL. No se añadieron migraciones.

## 14. Estado final de Git

Sin commit ni push. El documento de provisioning, ya sin seguimiento antes de esta tarea, queda editado; los demás cambios son de Block 13.2. Revisión directa del diff final, AST y `git diff --check` completados.

## 15. Recomendacion para la siguiente fase

Tras completar la revisión, autorizar explícitamente los recursos aislados de staging, provisionarlos sin datos reales y ejecutar el rehearsal del plan. No habilitar producción ni declarar `GO PILOT` hasta evidencia externa y aprobación de cada gate.
