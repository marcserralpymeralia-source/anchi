# Registro de gaps de preparación del piloto

Fecha de auditoría: 2026-10-01

Ámbito: Block 10, ANCHI-GEMAVI, worktree `modular-order-hub`
Decisión de alcance: auditoría y preparación operativa. Este documento no autoriza una remediation spree.

## Cómo leer el registro

- **P0**: detener el piloto; riesgo inmediato de pérdida de datos, exposición de secretos, cruce de tenants o ausencia de recuperación.
- **P1**: bloquea la salida del primer piloto hasta tener una decisión, una prueba de aceptación o una operación manual explícita.
- **P2**: no bloquea si queda fuera del contrato del piloto, pero debe tener dueño y fecha.
- **P3**: mejora de producto posterior.

Un gap se considera cerrado sólo cuando existe evidencia repetible en el mismo entorno que se va a utilizar para el piloto.

## Registro

| ID | Área | Hallazgo y evidencia | Severidad | ¿Bloquea piloto? | Reproducción/aceptación | Remediación sugerida | Esfuerzo | Dueño |
|---|---|---|---|---|---|---|---|---|
| G-01 | Storage | Los adjuntos y previews escriben en disco local. En Vercel sólo funcionan con Blob; fuera de Vercel dependen de `TEMP_STORAGE_DIR`. El full suite reproduce `PermissionError` al escribir `backend/app/storage/...`. | P1; P0 si no hay volumen persistente | Sí si el piloto recibe PDF/imagen | Escribir y leer un adjunto en el runtime real; reiniciar el servicio y comprobar lectura | Provisionar volumen persistente o Blob privado; validar permisos, capacidad y recuperación | M | Plataforma |
| G-02 | Readiness | `/health/ready` ahora ejecuta una prueba de storage local write/read/delete y rechaza Vercel sin `BLOB_READ_WRITE_TOKEN`; falta probarlo contra el runtime persistente del piloto. | P1 residual | Sí hasta validación de staging | Simular storage no escribible en el entorno final y exigir readiness no OK | Ejecutar la prueba en staging y conservar snapshot | S | Backend + Plataforma |
| G-03 | Calidad de release | Full suite: `673 tests`, `10 failures`, `2 errors`, `1 skipped`. Tres resultados tienen `PermissionError` de storage y deben quedar resueltos o excluidos con justificación firmada. | P1 | Sí para un piloto con adjuntos/imports | Repetir full suite en CI/runtime equivalente con cero errores no explicados | Corregir storage del entorno y aislar contaminación de suite | M | Backend + QA |
| G-04 | Email | El backfill IMAP captura `email.sync.message_error` y continúa; el test de PDF devuelve `saved=0` en la primera ejecución. El operador no recibe un detalle accionable por mensaje. | P2 si email está fuera; P1 si entra | No si email queda fuera del contrato | Aceptación IMAP real con PDF, segundo run idempotente y trazabilidad del error | Hacer visible el error por mensaje y resolver persistencia de adjuntos | M | Integraciones |
| G-05 | Email autodiscovery | Fallan individualmente: dominio bare sin candidato `mail.<domain>`, Gmail verificado y POP3-only. La detección depende de DNS, conectividad y probes concurrentes. | P2 | No si no se ofrece autodetección/email | Probar proveedores soportados desde el entorno real y guardar resultado sin password | Ajustar candidatos/probes o declarar configuración manual | M | Integraciones |
| G-06 | WhatsApp audio | `test_audio_download_waits_for_transcription_before_pipeline` falla individualmente (`downloaded=0`). | P2 | No si el piloto sólo usa Catalog estructurado | Excluir audio en configuración del piloto y rechazarlo con estado recuperable | Investigar descarga Meta/transcripción y repetir pruebas con media real | M | WhatsApp |
| G-07 | Workbench → entrega | `confirm_order_with_effects()` confirma y aprende, pero la ruta manual `/orders/{id}/confirm` no crea automáticamente un outbox. El outbox se crea en la entrada estructurada sin revisión; tras una revisión el operador debe exportar explícitamente. | P1 de workflow | Sí salvo que el SOP exija exportación manual | Pedido con duda → corrección → confirmar → verificar que se crea/encola la entrega esperada | Decidir y probar contrato: auto-encolar al confirmar o documentar exportación manual como paso obligatorio | S/M | Producto + Operaciones |
| G-08 | DLQ/entregas fallidas | Existe página `/orders/deliveries/failed` y retry POST tenant-scoped para Administrador/Supervisor, con auditoría e idempotency key; falta probarlo en staging y con un fallo real del destino. | P1 residual | Sí hasta aceptación en staging | Provocar fallo permanente, localizarlo por order/delivery y reintentar con evidencia de idempotencia | Ejecutar el escenario en staging y verificar alertas | S | Backend + Soporte |
| G-09 | Migraciones | Startup ejecuta/valida esquema y el worker salta tenants incompatibles. El flujo explícito existe y los tests de schema pasan 24/24, pero falta una ejecución real contra las bases del piloto. | P1 | Sí | `report-*`, `dry-run`, backup, upgrade, segunda ejecución no-op, readiness | Ejecutar migración previa al deploy y registrar versión/checksum por tenant | M | DBA/Plataforma |
| G-10 | Backup/restore | El repo documenta `pg_dump/pg_restore`, pero no configura backup, retención ni una restauración programada. | P1 | Sí | Restaurar master y tenant en staging, comprobar pedidos, jobs, mensajes y adjuntos | Política de backup gestionada, prueba de restore y RPO/RTO firmados | M | Plataforma |
| G-11 | Workers | El worker tiene claim, stale recovery, backoff y retries; el proceso es daemon/dedicado y en Vercel se deshabilita. Falta alerta externa de worker muerto o backlog envejecido. | P1 | Sí si el piloto depende de async | Parar worker, comprobar alerta, stale recovery y recuperación posterior | Monitor de heartbeat/backlog y un único worker activo por entorno | S/M | Plataforma |
| G-12 | Cron | `/cron/jobs`, `/cron/email-sync` y snapshots requieren `CRON_SECRET`; Vercel define schedules, pero no hay evidencia de ejecución del proveedor ni alerta ante 401/403/timeout. | P1 | Sí para jobs/crons activos | Invocar sin secreto/con secreto, comprobar logs y ejecución desde proveedor | Configurar secreto, synthetic check y alerta de última ejecución | S | Plataforma |
| G-13 | Configuración/deployment drift | Docker separa web y worker; Procfile también. La lista operativa de env vars no cubre todos los gates actuales (`TENANT_DB_MODE`, `ENCRYPTION_KEY`, `RUN_INTERNAL_*`, `CRON_SECRET`, storage, Meta). | P1 | Sí | Comparar manifest aprobado contra web, worker, cron y DB; arrancar con config inválida y verificar fail-fast | Manifest versionado y check de drift sin valores secretos | S/M | Plataforma |
| G-14 | Observabilidad | Hay audit tenant-scoped, correlation/request/job IDs y `/health/metrics`, pero los contadores son in-process y se pierden al reiniciar; no hay métrica durable de inbound, duplicados, backlog o latencia de negocio. | P2 | No para piloto pequeño si se conserva evidencia manual | Reiniciar y comprobar pérdida; reconstruir un pedido desde inbound → delivery → audit | Exportar métricas durables y dashboard mínimo | M | Plataforma |
| G-15 | Trazabilidad operativa | El flujo puede reconstruirse con audit/logs/jobs, pero no existe navegación unificada desde pedido a entrega, intento, HTTP y correlación. | P2 | No si soporte usa el runbook | Synthetic trace con un `correlation_id` y captura de IDs | Vista de timeline o enlace cruzado | M | Producto |
| G-16 | PII/retención | Se almacenan email, teléfono, mensajes, payloads y adjuntos; no se encontró política ejecutable de retención, borrado por tenant, exportación o offboarding. | P1 | Sí antes de datos reales | Tenant de prueba: exportar, desactivar, borrar/anonimizar según política y verificar blobs/logs/backups | Definir matriz de datos, retención, borrado y responsable | M | Seguridad/Legal |
| G-17 | RBAC | Hay roles/permisos y pruebas de aislamiento; no hay matriz de roles del piloto firmada ni revisión de cuentas de soporte/superadmin. | P1 | Sí antes de invitar usuarios reales | Matriz deny/allow para operador, supervisor, administrador y superadmin | Aprobar matriz, mínimo privilegio y revisión de accesos | S | Seguridad |
| G-18 | Browser/UI | Se ejecutó smoke browser local sobre despliegue sintético: login, tenant, pedidos, entregas fallidas y workbench; el runner `agent-browser` no estaba instalado y se usó browser nativo. Falta repetir en el navegador/proveedor final. | P1 residual | Sí para go-live | Navegador soportado: login, selección tenant, inbox, workbench, confirmación, job, logout | Repetir en runtime final y conservar evidencia | S | QA |
| G-19 | Full-suite: storage | `JobsReliabilityTests.test_import_confirm_retry_is_idempotent` y `RegressionFlowsTests.test_mock_order_uses_tenant_company_name` terminan con `PermissionError` al escribir preview/attachment. | P1 | Sí si storage forma parte del flujo | Repetir en entorno con storage permitido | Resolver G-01/G-03 | S/M | Backend + QA |
| G-20 | Full-suite: email | `EmailAiLearningTests.test_backfill_imap_deduplicates_pdf_attachment_on_second_run` falla con `saved=0` y `email.sync.message_error`. | P2/P1 según alcance | Sólo si email entra | Repetir con storage real y error visible | Resolver G-04 | M | Integraciones |
| G-21 | Full-suite: sesión | Cuatro tests de `test_multitenancy_auth` fallaron dentro del full suite con 303/selección de empresa, pero el módulo aislado pasa 4/4. | P1 de calidad de CI, no bloqueo funcional aislado | Sí hasta que CI sea determinista | Ejecutar módulo en proceso limpio y después full suite con aislamiento/reset | Identificar estado global/orden y hacer tests herméticos | M | QA/Backend |
| G-22 | Full-suite: autoconfig | Tres tests individuales fallan: fallback bare domain, proveedor conocido sin password y POP3-only. | P2 | No si email/autoconfig queda fuera | Prueba de red/DNS controlada o mocks contractuales | Separar discovery de red real y contrato mockeado | M | Integraciones |
| G-23 | Full-suite: onboarding | `SetupOnboardingTests.test_complete_email_onboarding_flow_with_imports_and_openai` falla en `/setup/products/preview` por `PermissionError` en `import_previews`. | P1 si onboarding/imports se usa | Sí si forma parte del piloto | Ejecutar onboarding completo con directorio persistente y limpieza validada | Resolver G-01 y repetir flujo | S/M | Backend |
| G-24 | Full-suite: audio | `WhatsAppIntegrationTests.test_audio_download_waits_for_transcription_before_pipeline` falla con `downloaded=0`. | P2 | No para Catalog-only | Excluir audio y verificar estado pendiente/rechazo; resolver antes de anunciar audio | Resolver G-06 | M | WhatsApp |

## Clasificación de la suite completa

Comando ejecutado previamente en entorno local:

```bash
rtk env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest discover -s backend/tests -p 'test*.py'
```

Resultado: `Ran 673 tests in 1055.545s`, `FAILED (failures=10, errors=2, skipped=1)`.

### Resultados individuales

| Resultado | Test exacto | Reproducción aislada | Clasificación |
|---|---|---|---|
| ERROR | `backend.tests.test_jobs_reliability.JobsReliabilityTests.test_import_confirm_retry_is_idempotent` | ERROR: `PermissionError` al escribir `backend/app/storage/import_previews/company-1/...csv` | Storage local no escribible |
| ERROR | `backend.tests.test_regression_flows.RegressionFlowsTests.test_mock_order_uses_tenant_company_name` | ERROR: `PermissionError` al escribir `backend/app/storage/attachments/tenants/1/...pdf` | Storage local no escribible |
| FAIL | `backend.tests.test_email_ai_learning.EmailAiLearningTests.test_backfill_imap_deduplicates_pdf_attachment_on_second_run` | FAIL aislado: `saved=0`, `email.sync.message_error` | Backfill/adjunto; confirmar con storage real |
| FAIL | `backend.tests.test_multitenancy_auth.MultiTenancyAuthenticationTests.test_configured_platform_owner_logs_into_superadmin_without_tenant_context` | PASS dentro del módulo aislado | Contaminación de estado/orden de suite |
| FAIL | `backend.tests.test_multitenancy_auth.MultiTenancyAuthenticationTests.test_incrementing_session_version_invalidates_previous_session` | PASS dentro del módulo aislado | Contaminación de estado/orden de suite |
| FAIL | `backend.tests.test_multitenancy_auth.MultiTenancyAuthenticationTests.test_selecting_membership_uses_the_selected_tenant_database` | PASS dentro del módulo aislado | Contaminación de estado/orden de suite |
| FAIL | `backend.tests.test_multitenancy_auth.MultiTenancyAuthenticationTests.test_user_with_two_memberships_is_sent_to_company_selection_after_login` | PASS dentro del módulo aislado | Contaminación de estado/orden de suite |
| FAIL | `backend.tests.test_settings_email_autoconfig.EmailAutoconfigTests.test_common_fallback_does_not_treat_bare_domain_as_mail_server` | FAIL aislado: falta el candidato `mail.<domain>` esperado | Discovery/probe |
| FAIL | `backend.tests.test_settings_email_autoconfig.EmailAutoconfigTests.test_known_provider_returns_verified_imap_without_password` | FAIL aislado: Gmail no resulta verificado con probes mockeados | Discovery/probe |
| FAIL | `backend.tests.test_settings_email_autoconfig.EmailAutoconfigTests.test_pop3_only_is_reported_but_not_marked_usable_by_anchi` | FAIL aislado: detección `False` | Discovery/probe |
| FAIL | `backend.tests.test_setup_onboarding.SetupOnboardingTests.test_complete_email_onboarding_flow_with_imports_and_openai` | FAIL aislado: `PermissionError` en `import_previews/company-1/...csv` | Storage/import preview |
| FAIL | `backend.tests.test_whatsapp_integration.WhatsAppIntegrationTests.test_audio_download_waits_for_transcription_before_pipeline` | FAIL aislado: `downloaded=0` | WhatsApp media/audio |

| Grupo | Casos | Clasificación | Decisión para el piloto |
|---|---:|---|---|
| Storage/imports/attachments | 3 errores/fallos, incluyendo setup onboarding | Fallo de entorno o disponibilidad de storage, no ocultar | Bloqueante si se usan PDFs/imports; validar G-01 antes de go-live |
| Email PDF backfill | 1 fallo | Fallo funcional/operativo pendiente de confirmar con storage real | Excluir email del primer contrato o bloquear por G-04 |
| Multitenancy auth | 4 fallos sólo en full suite; módulo aislado 4/4 OK | Contaminación de estado/orden de suite | No cerrar release hasta conseguir ejecución determinista |
| Email autoconfig | 3 fallos reproducibles aislados | Gap de discovery/probes y dependencia de red | Configuración manual o exclusión explícita |
| WhatsApp audio | 1 fallo reproducible aislado | Gap de media/transcripción | Fuera del primer piloto |

## Aprobación mínima

No hay P0 demostrado en el código auditado, pero el piloto se mantiene en **NO GO** hasta cerrar o aceptar formalmente como exclusión los P1 G-01, G-03, G-07, G-08, G-09, G-10, G-11, G-12, G-13, G-16, G-17 y G-18.
