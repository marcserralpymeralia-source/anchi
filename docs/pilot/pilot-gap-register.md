# Registro de gaps de preparación del piloto

Fecha de auditoría: 2026-10-01

Ámbito: Block 13, ANCHI-GEMAVI, worktree `modular-order-hub`
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

Resultado histórico Block 11: `Ran 673 tests`, `FAILED (failures=10, errors=2, skipped=1)`. Block 12 repitió la suite tras aislar storage/settings y corregir cron: `Ran 681 tests in 248.800s`, `FAILED (failures=4, skipped=1)`. Los 4 fallos son autodiscovery de email (3) y audio/transcripción (1), fuera del contrato Catalog-only.

### Resultados individuales del baseline Block 11 (histórico; sustituido por el addendum Block 12)

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

## Addendum Block 12 — clasificación vigente

| Estado vigente | Gaps | Evidencia |
|---|---|---|
| RESUELTO offline | G-19, G-21, G-23 | storage temporal por test, reset de `APP_ENV`/settings y previews resueltos; la suite ya no muestra esos fallos |
| RESUELTO offline, pendiente runtime | G-01, G-02, G-08, G-10, G-11, G-12, G-16, G-17 | `test_pilot_closure.py` 5/5, `test_mail_inbox_routes.py` + cierre 13/13, runbook y endpoint de offboarding |
| ABIERTO bloqueante | G-01/G-02 runtime persistente, G-08 staging, G-09 migración real, G-10 PostgreSQL/política gestionada, G-11/G-12 proveedor/alertas, G-13 drift, G-16 aprobación PII, G-17 matriz firmada, G-18 browser final | No ejecutado en el entorno final |
| FUERA DEL contrato Catalog-only | G-04/G-05/G-06/G-20/G-22/G-24 | Email/autodiscovery/backfill y audio no se incluyen; no se presentan como GO |

Los 4 fallos observados en la suite completa (3 de autodiscovery y 1 de audio) permanecen clasificados como features fuera del contrato, no ocultos ni convertidos en `skip`. El fallo de `/cron/jobs` autorizado fue una regresión de telemetría y queda corregido; la suite completa post-fix ya no lo reproduce.

## Aprobación mínima

No hay P0 demostrado en el código auditado, pero el piloto se mantiene en **NO GO** hasta cerrar o aceptar formalmente como exclusión los P1 G-01, G-02, G-08, G-09, G-10, G-11, G-12, G-13, G-16, G-17 y G-18. G-07 continúa sujeto al procedimiento explícito de confirmación/exportación; no se amplía en Block 12.

## Addendum Block 12.1 — validación final de runtime

La evidencia sintética adicional está documentada en [block-12-1-final-runtime-validation.md](./block-12-1-final-runtime-validation.md).

| Área | Estado Block 12.1 | Evidencia | Estado para piloto |
|---|---|---|---|
| PostgreSQL fresh install/upgrade | PASS sintético | Compose aislado, master/tenant migrados y segunda ejecución no-op | Repetir en proveedor final |
| Backup/restore PostgreSQL | PASS sintético | `pg_dump`/`pg_restore` master+tenant; checksums e invariantes restauradas | Política gestionada/RPO/RTO abiertos |
| Storage readiness | PASS local | probe write/read/delete y tests de aislamiento/fallo | Storage durable final OPEN; bloqueante |
| Web/worker separados | PASS sintético | procesos independientes, heartbeat y recuperación tras reinicio | Alerta externa pendiente |
| Scheduler/cron | PASS local condicionado | secreto, heartbeat previo, doble ejecución sin duplicación | Proveedor y alertas OPEN; bloqueante |
| Failed delivery/manual recovery | PASS sintético | browser mostró delivery fallida y retry; worker dejó error clasificado | Repetir con destino aprobado |
| Offboarding | PASS servicio/ORM sintético | canales 4/4 inactivos, memberships, audit e histórico conservado | Aprobación humana registrada; runtime final pendiente |
| RBAC/retención | APPROVED HUMAN / OPEN TECHNICAL | aprobación de Marc Serral registrada; discrepancias técnicas y runtime final pendientes | Bloqueante |
| Browser final | OPEN | navegador nativo local; confirmación final bloqueada por driver | Repetir en navegador/proveedor final |

La suite crítica del piloto terminó con **207 tests, 0 failures, 0 errors**. El full suite terminó con `681 tests, 4 failures, 1 skipped`; los cuatro casos son los tres fallos de email autodiscovery y el fallo de audio/transcripción ya clasificados como fuera del contrato Catalog-only. La decisión vigente permanece **NO GO**.

## Addendum Block 13 — Final Pilot Gate

La validación completa está documentada en [block-13-final-pilot-gate.md](./block-13-final-pilot-gate.md) y el contrato formal en [catalog-only-pilot-profile.md](./catalog-only-pilot-profile.md).

| ID | Área | Estado Block 13 | Evidencia | ¿Bloquea? |
|---|---|---|---|---|
| G-01/G-02 | Storage/readiness | OPEN | No existe runtime final con `write → restart → read`; sólo filesystem temporal sintético | Sí |
| G-08 | Failed delivery | PASS sintético / OPEN final | UI, retry, worker y audit validados sintéticamente; destino final no disponible | Sí hasta staging/final |
| G-09/G-10 | PostgreSQL/backup | PASS sintético / OPEN final | fresh/upgrade/restore heredados de Block 12.1; proveedor gestionado no validado | Sí |
| G-11/G-12 | Worker/cron/alerts | PASS local condicionado / OPEN proveedor | procesos separados, heartbeat y cron idempotente local; sin alertas/proveedor | Sí |
| G-13 | Runtime drift | OPEN | Docker/Vercel/Procfile describen topologías distintas; no existe manifest final enlazado | Sí |
| G-17 | RBAC | APPROVED HUMAN / OPEN TECHNICAL | aprobación de Marc registrada; `view_technical_logs` sigue heredado por Administrador | Sí |
| G-18 | Browser | OPEN | smoke local; sin runtime/browser final y confirmación offboarding bloqueada por driver | Sí |
| G-25 | Generic REST/SecretProvider | OPEN | `generic_rest` es `runtime_enabled=false`, `mock_only=true`, `future_secret_provider`; sin destino real autorizado | Sí |

Los fallos aislados de email autodiscovery y audio/transcripción siguen fuera del contrato y no bloquean por sí mismos el perfil Catalog-only. No se habilitó ningún módulo opcional ni se modificó código para ocultar resultados.

### Corrección de estado tras decisión del owner

Marc Serral aprobó expresamente RBAC y retention el 2026-10-01. La aprobación humana queda registrada, pero no se cierran automáticamente los gates técnicos:

| Gate | Estado humano | Estado técnico | Motivo |
|---|---|---|---|
| RBAC | `APPROVED FOR PILOT` | **OPEN** | `Administrador` hereda `view_technical_logs` y la cadena de permisos de Superadmin no contiene esa clave; la separación técnica no es limpia |
| Retention | `APPROVED FOR PILOT` | **OPEN** | No hay enforcement automático/runtime final de plazos, storage durable, backup gestionado o revocación externa |

## Addendum Block 13.1 — policy enforcement y configuration baseline

Fecha de implementación local: 2026-10-02. Detalle y evidencia: [block-13-1-policy-enforcement-configuration-baseline.md](./block-13-1-policy-enforcement-configuration-baseline.md).

| Gate | Estado local | Estado de piloto | Evidencia / límite |
|---|---|---|---|
| G-17 RBAC | **CERRADO LOCAL** | Runtime pendiente | Permisos tenant/platform separados; Administrador no recibe `view_technical_logs` ni `manage_tenants`; Superadmin no opera como usuario tenant; Supervisor conserva trazabilidad y retry; borrado de AuditLog denegado. |
| G-16 Retention | **RESUELTO LOCAL** | Runtime pendiente | Servicio tenant-scoped, expiración, dry-run, holds, gates y auditoría probados; `offboarded_at` usa migración aditiva. Scheduler/SecretProvider/storage/backup gestionados siguen pendientes del runtime. |
| G-13 Configuration drift | **RESUELTO LOCAL** | Baseline/runtime pendiente | Manifest hash, MATCH/DRIFT, campos críticos y readiness integrados. Sin baseline aprobado ni runtime final no se declara MATCH operativo. |
| Confirmación | **GUARD LOCAL** | Runtime pendiente | Revisión, cliente, productos, cantidad y blockers se validan en backend; sin aceptación browser final. |
| G-26 | Email: confirm/retry tras revisión | P2, fuera de Catalog-only | El guard bloquea dos regresiones de email que intentaban confirmar en `pedido_pendiente_revision`; el flujo de retry email termina con export no confirmado. Reproducido en 3 pruebas aisladas; Email necesita registrar transición explícita de revisión antes de habilitarse. No afecta Catalog-only. |

La decisión global permanece **NO GO**. No se cierran gaps de storage durable, Postgres/backup gestionado, cron/proveedor, alertas externas, Generic REST/SecretProvider real ni browser final. El informe Block 13.1 recoge los resultados finales de las pruebas.

## Addendum Block 13.2 — runtime adaptation local

Fecha: 2026-10-02. El estado siguiente supersede las referencias de implementación local en Block 13/13.1, pero **no** sustituye las pruebas de proveedor ni cierra el GO PILOT.

| Gap | Estado técnico local | Evidencia | Estado operativo restante |
|---|---|---|---|
| G-01 Storage | **READY FOR PROVISIONING** | Backend Blob seleccionado explícitamente, token SDK por operación, hostname privado exacto (`BLOB_STORE_HOST`), lecturas validan tenant y prefijo, fail-closed y ausencia de fallback filesystem; probe real bounded con write/read/verify/delete | **OPEN**: store privado/token dedicados; prueba cross-process `write → restart → read`, tenant denial y alertas |
| G-02 Readiness | **READY FOR PROVISIONING** | En perfil Catalog-only devuelve 503 ante DB/schema/storage/drift/gates inválidos; liveness separado; readiness sin tenant comprueba DB tenant activas | **OPEN**: demostrar sobre Render/Neon/Blob reales y comprobar alertas |
| G-09/G-10 PostgreSQL/restore | **READY FOR PROVISIONING** | CLI restore-postgres valida sidecar/checksum, identidad distinta, host/puerto/DB distintos, destino sin relaciones/rutinas/tipos/schemas de usuario, transacción única y `SELECT 1`; tests offline con subprocess/DB mocked | **OPEN**: migración y restore de master+tenant en branch Neon de recuperación vacía, invariantes y política/RPO/RTO |
| G-11 Worker | **READY FOR PROVISIONING** | Ambos perfiles deben coincidir exactamente; ausente/malformado/parcial en runtime no local procesa cero; `process_order`/Email reprocess quedan fuera; Email/internal worker deshabilitados y visibles en manifest/readiness; ambos webhooks dejan media en revisión sin downloader; SIGTERM/SIGINT y stale recovery/idempotencia | **OPEN**: reinicio Render, worker heartbeat/backlog y alerta sintética |
| G-12 Cron | **READY FOR PROVISIONING** | Invocador one-shot a `/cron/jobs`, HTTPS/Bearer con host `.onrender.com` exacto allowlisted, timeout explícito, sin redirects/secret en salida; ciclo bloqueado produce 503/heartbeat error | **OPEN**: crear sólo Cron `jobs`, probar 403/run idempotente/heartbeat y alerta del proveedor |
| G-13 Drift/config | **READY FOR PROVISIONING** | Manifest incluye backend storage, gates Generic REST/global HTTP/job profile/cron URL/host e internal workers; secretos no se serializan | **OPEN**: regenerar/aprobar baseline para valores finales y comparar servicios separados |
| G-25 Generic REST/SecretProvider | **READY FOR PROVISIONING; REMOTE PROBE OPEN** | Factory real reutiliza GenericRESTConnector, SecureHTTPTransport, EgressPolicy, SecretProvider environment-scoped y AuditLog; active connection y gates conjuntos; no mock fallback. Test de conexión queda BLOCKED sin probe configurado y no genera falso éxito | **OPEN**: aprobar sandbox/hostname/egress/secret/mapping, decidir probe seguro u operación sintética y ejecutar aceptación; ninguna llamada real emitida |

Los cambios sólo permiten preparar la topología; no se crearon recursos, no se transmitió tráfico, no se modificaron secretos y no se ejecutaron migraciones/backup contra Neon. G-08 delivery recovery, G-18 browser, aceptación de alertas y offboarding/histórico conservan sus estados de staging/final abiertos. No hay GO PILOT. El detalle de cambios, comandos y pruebas está en [pilot-staging-provisioning.md](./pilot-staging-provisioning.md#addendum-block-13-2--adaptaciones-locales-de-runtime).

#### Atribución de dos resultados del full suite — 2026-10-02

Los dos resultados adicionales a los fallos baseline y G-26 se reprodujeron dos veces individualmente y en su módulo. Ambos se clasifican como **PREEXISTING**, no como regresiones Block 13.2:

| Resultado | Test | Evidencia de causa | Clasificación / alcance |
|---|---|---|---|
| FAIL | `backend.tests.test_messages_and_conversations.MessagesAndConversationsTests.test_safe_whatsapp_auto_confirms_without_pending_review` | Fixture original (commit `c6c3e1c8`) usa extracción `lines=[]` y crea una orden sin cliente validado. `validate_confirmation()` requiere líneas y cliente validado; el flujo devuelve `auto_confirmed=False`, mantiene `pedido_pendiente_revision` y abre una revisión. El test y ruta de decisión son idénticos a HEAD. | **PREEXISTING**; desajuste entre fixture y expectativa. No lo causa runtime adaptation. No se cambió el test ni el comportamiento de confirmación en este bloque. |
| ERROR | `backend.tests.test_observability.ObservabilityTests.test_log_download_and_delete_are_scoped_to_company` | El test llama `delete_logs(db=db, user=user)`, que lanza `TypeError` porque la ruta existente `backend/app/logs/routes.py:189` sólo acepta `user`. El contrato actual niega la eliminación del AuditLog con HTTP 403. Test y ruta no están modificados por Block 13.2. | **PREEXISTING**; llamada de test incompatible con la firma/contrato vigente. Fuera del scope Catalog-only runtime adaptation. |
| FAIL | `backend.tests.test_orders_detail_optimization.OrdersDetailOptimizationTests.test_order_detail_query_budget_large` | Resultado del full suite: 16 queries frente al límite 15. Reproducción del mismo fixture: petición fría = 16 y segunda petición caliente = 15; si otra variante se ejecuta antes, el large también pasa. El GET `/orders/1` no cambia en el diff 13.2; los cambios en `orders/routes.py` se limitan a bloquear reprocess fuera de perfil y pasar `company_id` al leer adjuntos. | **TEST_ISOLATION**; el benchmark mide la primera inicialización como presupuesto steady-state. Hallazgo preexistente/fuera del scope 13.2, no regresión funcional Catalog-only. |

La suite final contabilizó `720 tests, 9 failures, 1 error, 1 skipped`: los cuatro fallos baseline (3 Email autodiscovery y 1 audio/transcripción), los tres casos G-26 Email documentados, estos dos resultados preexistentes y el caso de aislamiento cold/warm del benchmark. No se cambiaron tests de módulos ajenos a Block 13.2. La selección pilot-critical Catalog-only terminó con `229 tests, 0 failures, 0 errors`; los restantes resultados del full suite están fuera de ese perfil o son el benchmark cold/warm preexistente.
