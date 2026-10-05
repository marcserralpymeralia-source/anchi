# Plan de provisioning — pilot/staging Catalog-only

Fecha: 2026-10-02
Base documental: `feat/modular-order-hub`, Block 13.1 consolidado en `1e357a2`
Ámbito: plan técnico; sólo datos sintéticos; no autoriza provisioning, deploy, tráfico real ni producción.
Estado: **NO GO / provisioning aún no autorizado**

## Resumen ejecutivo

La arquitectura objetivo acordada es **Render Web + Render Background Worker + Render Cron + Neon PostgreSQL + Vercel Blob privado**. Render separa los tres procesos pedidos; Neon ofrece una base PostgreSQL aislada; Vercel documenta el uso de Blob desde runtimes externos pasando el token al SDK. No hay `render.yaml`, proyecto Neon ni configuración de proveedor existente en este worktree.

La topología es viable como objetivo, pero **no está lista para deploy**. Blob requiere una adaptación pequeña para ser seleccionado por configuración, usar explícitamente el token y fallar cerrado; Render Cron puede reutilizar `/cron/jobs`, pero no existe un invocador HTTP de una sola ejecución en el repo; readiness devuelve `200` aunque `ok=false`; y Generic REST real/SecretProvider no están habilitados. La decisión sobre Blob es **MINOR ADAPTATION**; eso no equivale a validación en Render ni a autorización para crear el store.

La rama/base Neon debe nacer sin parent con datos reales. No crear una branch desde producción: las branches Neon heredan por defecto el estado de la rama padre, incluido su contenido. Usar un proyecto Neon dedicado al piloto, branch raíz `pilot-staging` vacía y datos sintéticos únicamente. [Neon branching](https://neon.com/docs/introduction/branching)

## 1. Arquitectura objetivo

```text
Browser / Meta webhook sintético
          │ HTTPS
          ▼
Render Web Service (HTTPS público) ─┐
  FastAPI/Uvicorn                  │
  /health/live, /health/ready      │
          │                         │
          ├──── Neon branch pilot-staging
          │       ├── DB master     │ mismo endpoint privado de Blob
          │       └── DB tenant     │ y mismo token de store
          │                         ▼
          └──── Vercel Blob privado ◄──── Render Background Worker
                                             python -m app.workers.jobs_worker

Render Cron ── HTTPS + Bearer CRON_SECRET ──► Render Web /cron/jobs
```

**Región:** elegir explícitamente workspace/región Render y región Neon compatibles con política de datos/latencia. Web, worker y cron se crean en la misma región y workspace Render. Blob se configura como storage privado en el equipo/proyecto Vercel elegido; su región debe cumplir las condiciones aprobadas de residencia/contrato. Neon no debe reutilizar un proyecto, branch, role o URL de producción.

**Decisión de storage:** Vercel Blob privado, token compartido sólo entre los servicios que lo usan, con acceso restringido por la autorización de ANCHI y claves `tenants/{company_id}/attachments/...`. El prefijo es una convención de nombres, no una política IAM por tenant: la autorización y el acceso a la fila/ref deben seguir tenant-scoped en la aplicación. No almacenar adjuntos reales durante el piloto Catalog-only.

## 2. Configuración y comandos reales del repositorio

| Componente | Evidencia actual | Comando/equivalencia confirmada | Limitación |
|---|---|---|---|
| Imagen/build | `Dockerfile` en raíz; instala `backend/requirements.txt`, copia `backend/` y usa Python 3.13 | Render Docker runtime, Dockerfile `./Dockerfile`, contexto raíz. No hay build command separado configurado en repo. | `render.yaml` no existe. |
| Web | `Procfile` y `Dockerfile` | Procfile: `cd backend && APP_ENV=production uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`. Docker CMD: `uvicorn app.main:app --host 0.0.0.0 --port 8000`. | Si se conserva el Docker CMD, fijar `PORT=8000` en Render; alternativamente configurar el start command con el Procfile para usar el `PORT` asignado. No dejar un desacuerdo de puerto. |
| Worker | `Procfile` y `backend/app/workers/jobs_worker.py` | `cd backend && APP_ENV=production python -m app.workers.jobs_worker` (Procfile). En Docker, el working directory final ya es `/app/backend`, así que Render puede sobrescribir el CMD con `python -m app.workers.jobs_worker`. | Proceso continuo, no un Cron Job. Poll por defecto cada 10 s; recovery de job stale tras 900 s. |
| Scheduler | `backend/app/cron/routes.py`, `vercel.json` | Endpoint HTTP `GET` o `POST /cron/jobs`; `vercel.json` actual lo programa como `*/5 * * * *`. | No hay comando/cliente de una ejecución en Render ni script cron independiente. Vercel configura además email-sync y platform-snapshots, ambos excluidos del piloto. |
| Migración master | `backend/scripts/schema_migrations.py` | `PYTHONPATH=backend python3 backend/scripts/schema_migrations.py upgrade-master` | Mutación DB; sólo ejecutar con autorización, URLs Neon staging verificadas y evidencia del backup/snapshot. |
| Informe master | mismo CLI | `PYTHONPATH=backend python3 backend/scripts/schema_migrations.py report-master` | Lectura/report; conectar sólo a Neon staging. |
| Migración tenant | mismo CLI | `PYTHONPATH=backend python3 backend/scripts/schema_migrations.py upgrade-tenant --company-id <PILOT_COMPANY_ID>` | Requiere que master ya tenga el mapeo tenant→URL. Usar el ID sintético confirmado, nunca `upgrade-all-tenants` en un entorno compartido. |
| Informe tenant | mismo CLI | `PYTHONPATH=backend python3 backend/scripts/schema_migrations.py report-tenant --company-id <PILOT_COMPANY_ID>` | Sólo después de provisionar tenant en la branch piloto. |
| Backup DB | `backend/scripts/pilot_backup_restore.py` y `docs/pilot/pilot-backup-restore-runbook.md` | `PYTHONPATH=backend python3 backend/scripts/pilot_backup_restore.py backup --database-url "$MASTER_DATABASE_URL" --output <ARTEFACTO> --environment staging --identity master`; repetir con URL tenant e identidad `tenant:<id>/<slug>`. | El binario requiere `pg_dump` instalado. El Dockerfile actual no instala explícitamente PostgreSQL client. La salida es filesystem local; no hay uploader a Blob en el repo. |
| Restore DB | `backend/app/core/backup.py` | Hay helper PostgreSQL `pg_restore()`; **no** hay subcomando CLI PostgreSQL. El CLI sólo ofrece `restore-sqlite`. | Antes del ensayo hace falta definir una ruta reproducible de restore PostgreSQL en branch/DB vacía, validar checksum y registrar resultado; no restaurar sobre la DB activa. |
| Health | `backend/app/health/routes.py` | `GET /health/live`, `GET /health/ready` | `/health/ready` incluye DB/schema/storage/drift/operational, pero hoy devuelve HTTP 200 incluso cuando el JSON contiene `ok=false`; no usarlo como health-check de Render hasta corregirlo. |

Las páginas de desarrollo que citan `backend/scripts/demo_ops.py` no son evidencia de comando actual: ese archivo no aparece en el inventario de `backend/scripts/`. El entrypoint de migración verificable es `backend/scripts/schema_migrations.py`.

`docker-compose.yml` es sólo stack local: PostgreSQL 16, volumen Docker y credenciales literales de desarrollo. No reutilizarlo ni sus credenciales como provisioning cloud.

## 3. Render Web

- Crear un Web Service con URL HTTPS pública para browser y webhook de staging, protegido por autenticación/CSRF y configuración de host; no es un Render Private Service. Limitarlo al scope del piloto, desde la rama `feat/modular-order-hub`/commit aprobado, Dockerfile raíz y contexto raíz. No activar auto-deploy hasta que se autorice expresamente el despliegue.
- Bind público HTTP en `0.0.0.0`; HTTPS termina en el edge Render. El proceso del repo escucha en `8000`; definir Render `PORT=8000` si se usa el Docker CMD. Render permite cambiar el valor de `PORT`; la alternativa es usar el comando `web` del Procfile que ya lo consume. [Render Web Services](https://render.com/docs/web-services)
- Health-check de proceso: `/health/live` sólo detecta vida del proceso. El health-check de readiness para el deploy debe ser `/health/ready` **después** de hacer que estados no-ready produzcan HTTP no-2xx. Render considera 2xx/3xx exitoso, no inspecciona el JSON `ok` por sí mismo. [Render health checks](https://render.com/docs/web-services#health-checks)
- `RUN_INTERNAL_JOB_WORKER=false` en Web para evitar un poller de jobs duplicado en el proceso web; `RUN_INTERNAL_EMAIL_WORKER=false`; `APP_ENV=production`, `DEBUG=false`, `ENABLE_DEMO_BOOTSTRAP=false`.
- Sin disco Render para adjuntos: el filesystem por defecto es efímero y el disco de un servicio no es compartido con los otros. El uso de Blob evita depender de ese disco. Un disco local sólo sería un almacenamiento de un único servicio, no una solución Web↔Worker. [Render persistent disks](https://render.com/docs/disks)
- Restart esperado: Web vuelve a estar healthy, reconecta a Neon/Blob; no debe depender de estado local. Confirmar sesión/cookie y que el reinicio no cambia el manifest/config aprobado.

## 4. Render Background Worker

- Crear exactamente un Background Worker, misma fuente/revisión, Dockerfile, región, DB master y tenant, env group y referencia al mismo Blob store que Web.
- Comando repo: `python -m app.workers.jobs_worker` con working directory `/app/backend` en Docker. En runtime nativo, usar Procfile `worker` y build `pip install -r backend/requirements.txt`; esta alternativa no tiene definición de servicio versionada en el repo.
- El proceso es un loop que ejecuta `run_worker_cycle(include_provisioning=True)` y duerme `JOB_POLL_INTERVAL_SECONDS` (alias; default efectivo 10 s, mínimo validado 5). No atiende HTTP ni requiere puerto.
- `RUN_INTERNAL_JOB_WORKER` controla el worker embebido en FastAPI, no convierte el comando de Background Worker en no-op. Usar el proceso separado como consumidor; no arrancar una segunda instancia en staging mientras se verifica el propietario único.
- El worker heartbeat se registra en Neon master como `worker/jobs`; readiness lo considera stale después de 5 minutos. El lease/recovery local de jobs usa `JOB_STALE_AFTER_SECONDS` (default 900). Render envía SIGTERM antes de parar; el loop actual no implementa un protocolo explícito de drenaje SIGTERM. Validar restart con job sintético/idempotency key y añadir graceful shutdown antes de aceptar concurrencia externa si la prueba muestra posibilidad de reejecución ambigua.
- El worker maneja más tipos que los incluidos en Catalog-only (incluye jobs de email). El manifest de módulos no filtra por sí mismo la cola: Email debe quedar desactivado en el tenant y no deben encolarse jobs opcionales. Añadir/verificar una allowlist de tipos Catalog-only en el worker es un gate antes de activar tráfico, no una feature incluida en este plan.

## 5. Render Cron → endpoint HTTP protegido

**Reutilización: viable; preferirla a implementar otro scheduler.** El repo ya expone `GET`/`POST /cron/jobs`, exige `CRON_SECRET` y registra heartbeat/error `scheduler/jobs`. `/cron/jobs` llama `run_worker_cycle(max_jobs=...)`; el claim transaccional, locks y recuperación stale existentes aportan idempotencia concurrente. Render Cron puede ejecutar un comando shell finito que haga una petición saliente HTTPS al endpoint Web y termine; los cron Render no reciben tráfico entrante y no tienen disco persistente. [Render Cron Jobs](https://render.com/docs/cronjobs)

- Crear un único Cron Job `catalog-jobs` con horario inicial heredado de `vercel.json`: `*/5 * * * *` **UTC**. No crear cron para `/cron/email-sync` ni `/cron/platform-snapshots`: no son necesarios para el perfil Catalog-only y el repo no demuestra dependencia. Mantener `PILOT_REQUIRED_SCHEDULER_JOBS=jobs`.
- Trigger: `GET https://<render-web-host>/cron/jobs` o `POST` equivalente; header `Authorization: Bearer <CRON_SECRET>` (el endpoint también acepta `x-cron-secret`). Falta un cliente/entrypoint de una ejecución en el repo: el comando exacto del Render Cron sigue pendiente. Añadir un pequeño invocador Python basado en `urllib` que lea `CRON_SECRET` del entorno, llame sólo `/cron/jobs`, falle con exit code no-cero en HTTP/timeout y nunca imprima el secreto; o probar un command built-in de proveedor sin añadir dependency runtime. No inventar un `curl` presente: el Dockerfile no lo instala.
- `CRON_SECRET` debe estar definido; el endpoint devuelve 403 si falta o no coincide. Los fallos HTTP del caller deben aparecer como ejecución fallida en Render, y el endpoint persiste estado/heartbeat. Configurar alerta sobre el resultado de cada run y sobre ausencia/stale heartbeat (>5 min), más verificación explícita 403 sin credencial.
- Debe haber exactamente un Render Cron `jobs`. `vercel.json` programa tres cron del proveedor Vercel; Render no consume ese archivo. Mantener la aplicación staging sólo en Render y confirmar que no haya otro deployment/cron que invoque la misma DB.
- Worker y `/cron/jobs` pueden intentar consumir la cola; el claim/lock y la idempotencia deben probarse con una ejecución solapada. La garantía single-run de Render Cron cubre sólo ese cron service, no el worker separado.

## 6. Neon PostgreSQL aislado

- Crear, con aprobación, un proyecto Neon dedicado `anchi-pilot-staging`; crear la branch `pilot-staging` desde la branch default recién creada y vacía, nunca desde una parent de producción. No clonar una branch con datos reales. Neon branches normales heredan el estado de su parent, por lo que “crear branch” no significa “crear base sin datos”. [Neon branching](https://neon.com/docs/introduction/branching)
- Crear `anchi_master` y una DB de tenant piloto separada dentro de esa branch, con roles de aplicación/migración de mínimo privilegio y URLs distintas. Un solo tenant sintético; guardar el mapeo de su DB en master mediante el flujo de onboarding autorizado. No reutilizar `MASTER_DATABASE_URL`, `TENANT_DATABASE_URL`, role, endpoint ni secret de otro entorno.
- La app requiere `MASTER_DATABASE_URL`, `TENANT_DB_MODE=external` y URL tenant (`TENANT_DATABASE_URL` al bootstrap; el master almacena URL por tenant). Runtime puede necesitar un endpoint pooler según la carga/conexiones; migraciones, dumps y restore deben usar conexión **directa/no pooled**. Elegir TLS, límites de conexión y retención de historial con el owner del proyecto.
- Instalar esquema desde cero: `upgrade-master`; crear/provisionar el tenant sintético y su mapping; `upgrade-tenant --company-id <id>`; después `report-master` y `report-tenant`. El CLI no crea el proyecto/branch/DB ni hay un script de bootstrap nuevo de tenant visible en `backend/scripts`; resolver el paso de onboarding antes del rehearsal.
- Neon permite branching/PITR según la ventana/configuración del proyecto; verificar en el plan contratado la retención efectiva y restore antes de declarar backup aprobado. El flujo de recuperación del ensayo debe crear una branch/DB aislada de recuperación, validar invariantes y no reemplazar/destruir la branch activa. Para restore operacional puntual, registrar commit/schema/checksum, RPO/RTO y aprobación humana.

## 7. Vercel Blob privado desde Render — **MINOR ADAPTATION**

### Evidencia actual

- `backend/app/core/attachment_storage.py::_use_vercel_blob()` sólo selecciona Blob cuando detecta `VERCEL=1` o `VERCEL_ENV`, además de que `BLOB_READ_WRITE_TOKEN` esté presente.
- `save_attachment()` llama `BlobClient().put(..., access="private")`; `read_attachment()` reconoce refs HTTP(S) y llama `BlobClient().get(..., access="private")`. No se pasa `token=` explícitamente.
- Fuera de Vercel, incluso si existe el token, el write cae al filesystem (`TEMP_STORAGE_DIR` o `backend/storage`); `check_storage_readiness()` también prueba filesystem local. En Vercel sin token, el write sí falla; fuera de Vercel un fallo de selección puede aparentar éxito en almacenamiento efímero. Esto es fallback silencioso no admisible en staging.
- La dependencia `vercel==0.10.0` ya está en `requirements.txt`. La documentación oficial admite runtimes fuera de Vercel pasando el token al método del SDK. `put`, `get`, `head`/delete aceptan token; el store determina el ámbito de acceso y Blob privado requiere el token para leer/escribir. [Vercel Blob SDK](https://vercel.com/docs/vercel-blob/using-blob-sdk), [Blob privado](https://vercel.com/docs/vercel-blob/security)

### Cambio mínimo previo

1. Añadir selector explícito, p. ej. `ATTACHMENT_STORAGE_BACKEND=vercel_blob`; no inferir el backend con `VERCEL`/`VERCEL_ENV`.
2. Construir/llamar Blob pasando explícitamente `BLOB_READ_WRITE_TOKEN` a put/get (y al probe delete); conservar `access="private"` y el `BlobClient.close()` existente. Confirmar compatibilidad exacta con la versión pinneada `vercel==0.10.0` mediante test local antes del despliegue.
3. En `save_attachment`, `read_attachment` y readiness, backend `vercel_blob` sin token, error de API, timeout o mismatch debe lanzar/reportar fallo. **Nunca** volver a filesystem automáticamente. Filesystem sólo se permite cuando esté seleccionado explícitamente para tests/desarrollo y no puede satisfacer readiness staging.
4. Cambiar readiness Blob de “token configurado” a probe real `write → read → delete` privado con objeto aleatorio de probe; registrar resultado sin URL/token/payload sensible. El test de aceptación debe guardar un objeto sintético, reiniciar Web/Worker y leerlo desde el otro proceso/instancia.
5. Incluir backend storage efectivo en `PilotConfigurationManifest` como campo crítico de drift; ahora el manifest sólo refleja presencia booleana de `BLOB_READ_WRITE_TOKEN`, no la selección de backend.

Web y Worker pueden compartir el **mismo store/namespace** si ambos reciben el token Read/Write de ese mismo store; no se necesita sincronizar filesystem. El path de subida ya incluye `tenants/{company_id}/attachments`. Ese prefijo y la separación de DB son aislamiento lógico de la app, no credenciales distintas por tenant: probar que una sesión/tenant no puede resolver un ref de otro tenant y no exponer URL de Blob privada a usuarios sin autorización. Cron de jobs no requiere token Blob mientras no procese/storage attachments; no inyectarlo por defecto.

Ante indisponibilidad de Blob: no escribir local, fallar el upload/job según política retryable existente, emitir error sanitizado, storage readiness no saludable y alerta. La adaptación es pequeña por el SDK ya presente y API compatible; la prueba de token/namespace multi-proceso en Render y los controles de aislamiento aún son obligatorios. No queda aprobado almacenamiento duradero hasta demostrar `write → restart → read` en staging.

## 8. SecretProvider y Generic REST

- Boundary actual: `backend/app/connectors/security.py` define `SecretProvider` y el default `EnvironmentSecretProvider`. Acepta `credential_ref=secret://tenant-N/connection-N/name` y resuelve `ANCHI_SECRET_TENANT_N_CONNECTION_N_NAME`; valida que tenant y connection coincidan. La credencial no debe aparecer en `config_json`, manifest, logs o URLs.
- Para una primera sandbox se puede inyectar el env key como secret de Render compartido por Web/Worker con scope de mínimo privilegio. Esto es un provider de entorno existente, **no** integración con Secret Manager API ni rotación dinámica. Si la política exige un provider gestionado externo a los servicios, implementarlo/seleccionarlo antes del destino real. Nunca guardar valor en Neon.
- El manifest informa `PILOT_SECRET_PROVIDER` pero el código efectivo por defecto es `EnvironmentSecretProvider`; alinear el valor informado con el provider realmente instanciado y validar drift.
- `generic_rest` está registrado con `runtime_enabled=false`, `mock_only=true`, `factory_path=...build_generic_rest_mock_connector` y `credentials_source=future_secret_provider`. En `APP_ENV=production` el registry rechaza mock-only. `EXTERNAL_CONNECTOR_HTTP_ENABLED` es una compuerta del transporte seguro, no hace que el registry instancie el transporte real ni convierte Generic REST en runtime-enabled. El manifest actual toma `PILOT_HTTP_CONNECTOR_ENABLED`, no esa variable efectiva.
- Antes de usar un único destination real/sandbox hacen falta: factory/runtime real del Generic REST; resolución efectiva de credenciales desde provider; mapping validado; `EXTERNAL_CONNECTOR_HTTP_ENABLED=true` sólo para la ventana aprobada; HTTPS, allowlist exacta, DNS rebinding/egress checks; destination sandbox con idempotency key remota; límites/timeouts; test de fallo/transient/terminal/retry y no duplicado; manifest que registre la compuerta efectiva. No habilitar Sage/Odoo/email/audio.

## 9. Variables/configuración (nombres, sin valores)

| Grupo | Variables / configuración | Aplicación |
|---|---|---|
| Runtime seguro | `APP_ENV=production`, `DEBUG=false`, `ENABLE_DEMO_BOOTSTRAP=false`, `SECRET_KEY`, `ENCRYPTION_KEY`, `APP_URL`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `SESSION_COOKIE_SECURE=true`, `CSRF_PROTECTION_ENABLED=true` | Web; claves gestionadas, distintas de cualquier entorno. El modelo `Settings` sólo admite `development/demo/test/production`, no `staging`; no usar `APP_ENV=demo` porque activa demo bootstrap por diseño. |
| DB | `MASTER_DATABASE_URL`, `TENANT_DB_MODE=external`, `TENANT_DATABASE_URL` (bootstrap de tenant; aparte, URL de tenant almacenada en master), `DATABASE_URL` sólo compatibilidad | Web/Worker/Cron; valores Neon exclusivos de pilot-staging. Runtime URLs separadas por DB. |
| Worker/scheduler | `RUN_INTERNAL_JOB_WORKER=false` en Web, `RUN_INTERNAL_EMAIL_WORKER=false`, `JOB_POLL_INTERVAL_SECONDS`, `JOB_MAX_ATTEMPTS`, `JOB_RETRY_BASE_SECONDS`, `JOB_RETRY_MAX_SECONDS`, `JOB_STALE_AFTER_SECONDS`, `CRON_JOB_BATCH_SIZE`, `PILOT_REQUIRED_SCHEDULER_JOBS=jobs`, `CRON_SECRET` | El Cron sólo recibe el secreto necesario; Web compara token. Worker mantiene sólo una instancia operativa. |
| Pilot manifest | `PILOT_PROFILE=catalog-only`, `PILOT_ENABLED_MODULES`, `PILOT_DISABLED_MODULES`, `PILOT_CONFIGURATION_BASELINE_JSON`, `PILOT_COMMIT_SHA` | Igual perfil/baseline entre servicios. Baseline sanitizado, aprobado, sin credenciales; incluir storage y gates efectivos tras el cambio mínimo. |
| Storage | `ATTACHMENT_STORAGE_BACKEND=vercel_blob` (nuevo), `BLOB_READ_WRITE_TOKEN` | Token sólo en Web/Worker si las pruebas de proceso lo requieren. No exponer a frontend/logs/manifest. Selector aún no existe. |
| WhatsApp | `META_APP_ID`, `META_APP_SECRET`, `META_EMBEDDED_SIGNUP_CONFIG_ID`, `META_WHATSAPP_VERIFY_TOKEN`, `META_WHATSAPP_REGISTRATION_PIN`/config de callback sólo si el flujo aprobado los exige | Inyectar sólo credenciales Meta de staging/sandbox; no reusar apps/tokens de producción. |
| Connector secret | `PILOT_SECRET_PROVIDER`, `ANCHI_SECRET_TENANT_<id>_CONNECTION_<id>_<NAME>` (pattern actual) | Sólo secret env administrado, no `.env` versionado; Web/Worker deben compartir scope. |
| Temporales | `TEMP_STORAGE_DIR` | Sólo cachés/previews temporales. Nunca configurarlo como solución para adjuntos durables. |

No asumir que `IMAP_SYNC_ENABLED`/`EMAIL_AUTO_SYNC_ENABLED` controlan módulos: no se encontraron como gates consumidos por runtime. Desactivar el canal Email en el tenant, desactivar el email worker y mantener fuera de la cola los jobs opcionales; añadir guardas verificables si la allowlist Catalog-only no basta.

## 10. Health/readiness y manifest

- `/health/live`: proceso web responde; usarlo para liveness, no como señal de migraciones/storage.
- `/health/ready`: consulta master, schema, tenant si la petición tiene tenant context, storage, configuration drift y estado operativo. Los heartbeats worker/scheduler se consideran stale a los 5 minutos. El endpoint hoy construye JSON con `ok`; no fija status HTTP no-2xx cuando false. **Cambio requerido:** devolver 503 (u otro 5xx acordado) para readiness false e incluir razones sanitizadas; probar schema pendiente, DB caída, Blob caída y drift crítico.
- Render `healthCheckPath` puede ser `/health/ready` después de ese cambio. Mientras tanto monitor HTTP 200 no demuestra ready y no puede gatear el deploy.
- Guardar hash/manifest aprobado antes y después de migración y tras la configuración de una conexión. `PILOT_CONFIGURATION_BASELINE_JSON` vacío da `NOT_CONFIGURED`, no `MATCH`; baseline inválido/crítico debe bloquear readiness. Comparar Web, Worker y Cron: commit, schema, módulos, connector, rutas, RBAC, retention, gates y backend de storage.
- `/health/ready` sin contexto de tenant sólo acredita master; para tenant DB/failed-delivery count usar sesión de acceptance autenticada y guardar el snapshot sanitizado correspondiente.

## 11. Alertas

La app expone señales, pero no configura reglas/notificaciones externas. “SUPPORTED” significa señal existente en código, no alerta ya provisionada.

| Incidente | Señal del repo | Estado de señal | Configuración de alerta requerida |
|---|---|---|---|
| Web unhealthy | `/health/live`, `/health/ready` | `SUPPORTED`; readiness HTTP status hoy `GAP` | Monitor externo + alerta Render; usar readiness tras corregir 200/`ok=false`. |
| Worker muerto/stale | `operational.worker`, `worker/jobs`, stale >5 min | `SUPPORTED` | `REQUIRES CONFIG`: consulta/monitor autenticado del status operativo o chequeo aprobado; alertar en stale/error. |
| Scheduler failure/stale | `operational.scheduler.required_jobs.jobs`, heartbeat/error, stale >5 min | `SUPPORTED` | `REQUIRES CONFIG`: alerta por run Render fallido/HTTP non-2xx y ausencia heartbeat. |
| PostgreSQL no disponible | ping master/tenant en readiness | `SUPPORTED` parcial | `REQUIRES CONFIG`: alerta Neon/monitor; incluir tenant ping real, no sólo master. Corregir readiness HTTP code. |
| Blob no disponible | actual en Render sólo filesystem local; Blob Vercel sólo valida token config | `GAP` hasta adaptación/probe real | Tras implementar probe, `REQUIRES CONFIG`: alerta readiness no saludable y errores de objeto en logs. |
| Delivery terminal failed | contador `failed_deliveries` sólo cuando readiness tiene tenant DB context | `SUPPORTED` parcial | `REQUIRES CONFIG`: métrica/consulta tenant-scoped y threshold/antigüedad; hoy no hay alerta external ni antigüedad de backlog. |
| Backup failed/restore | sidecar/checksum y errores CLI, sin heartbeat/alerta backup integrada ni upload de artefacto | `GAP` | Definir ownership de Neon snapshots/PITR y job de export; configurar alerta del proveedor/run; ensayar restore. |

Render run history/logs y Neon branch/database observability son evidencia del proveedor a habilitar en sus consolas. No declarar `alerts PASS` hasta causar una señal sintética, recibir notificación y dejar captura/ID/runbook.

## 12. Browser acceptance

No se encontró Playwright, Selenium, `package.json` de tests ni harness E2E browser en el repo. Tests Python cubren flows de dominio, no sustituyen el recorrido real. Usar browser manual soportado o definir un harness separado sujeto a autorización; no añadir `agent-browser` como dependency runtime.

Recorrido staging con usuario/tenant sintéticos y capturas/IDs: `Login → Tenant → Onboarding → Integrations → Orders → Workbench → Failed Deliveries → Retry → Audit → Pause/Resume`. Confirmar roles Operator/Supervisor/Admin, sin exposición de cross-tenant, y que sólo Admin/Supervisor puede recovery según matriz vigente. Capturar consola/network sin tokens; anotar correlation ID, order/delivery/job/attempt/audit y manifest hash. No marcar E2E automático PASS si fue un recorrido manual.

## 13. Secuencia de deployment (plan, aún no ejecutar)

1. Resolver blockers de código descritos en §16, ejecutar suite relevante y revisar diff/approval separado.
2. Obtener autorizaciones humanas de workspace/owner/region/coste/retención/residencia de datos; validar política de Neon/Vercel Blob y responsable on-call.
3. Crear un proyecto Neon aislado y vacío; crear `pilot-staging` desde su branch default vacía y DBs master/tenant; registrar endpoints y retención aprobados sin copiarlos al repo.
4. Crear Blob Store **Private** vacío para staging y gestionar el token sólo en el secret manager. Comprobar que Web y Worker obtienen la referencia al mismo store/token; no mostrarlo ni pegarlo en documentación.
5. Crear Render env group `anchi-pilot-staging` sin valores compartidos con producción; configurar Web, un Worker y un Cron Job en la misma región. Usar Dockerfile raíz o Procfile de manera consistente; desactivar auto-deploy hasta aprobación.
6. Definir/registrar migración de provisioning y flujo para crear master company, tenant DB mapping, usuario/admin sintéticos, permisos y baseline. Confirmar las URLs pertenecen al proyecto/branch Neon staging mediante identidad de proyecto/branch antes de escribir.
7. Aplicar master migration y tenant migration con conexión directa; verificar report-master/report-tenant `current`, esquema/checksum y ausencia de migraciones pendientes. Snapshot previo.
8. Construir manifest Catalog-only aprobado; configurar conexión Generic REST sólo cuando su runtime/provider esté implementado. Si aún es mock-only, no activar destino de piloto ni llamar el gate cerrado.
9. Desplegar Web a staging manualmente con `PORT` coherente; esperar health/readiness no-2xx correcta y revisar logs. Después arrancar Worker separado; verificar heartbeat y un solo worker owner.
10. Crear un único Render Cron para `jobs`; invocar primero manualmente con auth correcta y luego por schedule UTC. Confirmar 403 sin/credencial incorrecta, un heartbeat, resultado idempotente y sin cron duplicado en otro proveedor.
11. Ejecutar Blob `write → restart → read` cruzando Web/Worker; forzar Blob error sin fallback; comprobar readiness y alerta.
12. Browser critical path; backup/export, restore aislado, reinicios y rehearsal §14. No usar tráfico/PII real.

## 14. Rehearsal final (no ejecutado)

1. Confirmar health/manifest, backup/snapshot, tenant y credenciales sólo sintéticos; verificar Database identity/branch en los servicios.
2. Crear tenant sintético y completar onboarding; instalar un catálogo reducido con un único Generic REST sandbox aprobado e idempotency key. Si Generic REST no está habilitado, parar aquí: sólo se permite aceptación mock sin llamarla real.
3. Inyectar WhatsApp Catalog sintético válido; procesar **20 pedidos correctos**; conciliar inbound/order/delivery/audit y external ID.
4. Repetir inbound duplicado; comprobar un solo pedido/job/delivery externo.
5. Probar unknown product/customer; debe quedar en revisión sin export hasta resolución auditada por Supervisor/Admin.
6. Simular fallo transitorio REST, backoff/retry y recuperación; no duplicar destino.
7. Simular fallo terminal, mostrarlo en Failed Deliveries, ejecutar Retry manual, verificar attempt/audit e invariantes.
8. Detener/reiniciar Web; probar login/readiness y conservar referencias Blob. Detener/reiniciar Worker en un job sintético; revisar stale recovery, resultado final e idempotencia.
9. Reiniciar scheduler/Render Cron (deshabilitar y volver a habilitar según runbook); comprobar una sola ejecución y heartbeat. Probar auth 403 y alerta de fallo.
10. Reinicio completo: Web + Worker + Cron; asegurar conexiones a Neon/Blob, baseline MATCH y no duplicado.
11. Blob `write → restart (Web/Worker) → read`; probar tenant B no accede a referencia tenant A y provocar indisponibilidad sin fallback filesystem.
12. Ejecutar backup master+tenant o snapshot aprobado; restaurar a branch/DB Neon vacía de recuperación; validar filas, schema/checksum, tenant links, audit, exported no re-export, failed delivery retry sin segunda entrega. No sobreescribir staging activo.
13. Probar offboarding: pause input/output/jobs, preservar pedidos/deliveries/attempts/audit, revocar referencias de secreto fuera de DB y confirmar no nuevos jobs. Verificar retención/histórico; no borrar datos de negocio.
14. Repetir no-duplicate invariant para inbound repetido, timeout tras éxito remoto, worker restart y scheduler solapado. Guardar evidencias sanitizadas.

## 15. Recursos cloud a crear (sólo tras autorización)

1. Render Web Service ANCHI pilot/staging.
2. Render Background Worker único `jobs`.
3. Render Cron Job único `jobs`, cron expression UTC, sin `email-sync`/`platform-snapshots`.
4. Neon project aislado, branch raíz vacía `pilot-staging`, DB master + DB de un tenant, roles separados/runtime y migrator role.
5. Vercel Blob Store **Private** dedicado a staging, token Read/Write asociado a ese store.
6. Secret store/env group Render para app keys, DB URLs, CRON_SECRET, Blob token, Meta sandbox, eventual Generic REST secret; acceso nominativo/mínimo.
7. Monitor/alert routing para Render web/worker/cron, Neon DB y Blob/readiness; browser/evidence store seguro sin secretos.
8. Dominio staging opcional y DNS sólo si el browser/webhook acceptance lo necesita; no registrar ni cambiar dominios reales sin aprobación.

No hace falta crear Render Postgres: la arquitectura elegida usa Neon. No hace falta un Render disk para Blob; no usarlo como almacenamiento común. No crear Redis/Key Value: el worker usa la DB/jobs actuales, no un broker Redis en el código inspeccionado.

## 16. Cambios de código mínimos requeridos antes de deploy

1. **Blob selector/fail-closed/readiness** (§7), explícito por env, token pasado al SDK, probe write/read/delete real, sin fallback a FS, cross-process/tenant tests y storage backend incluido en manifest. Clasificación: **MINOR ADAPTATION**.
2. Readiness que responda HTTP no-2xx ante `ok=false`; el endpoint actual puede hacer que Render marque ready un servicio que reporta storage/schema/drift no saludable.
3. Invocador one-shot Render Cron basado en HTTP existente (`/cron/jobs`) que transmite Bearer sin imprimirlo, valida respuesta/timeout y termina con exit status fiable. No implementar otro scheduler.
4. Asegurar enforce Catalog-only en worker/queue: `PILOT_ENABLED_MODULES` por sí solo es manifest; worker acepta job types opcionales y puede procesar jobs Email si existen.
5. Generic REST real: registro/factory runtime-enabled, wiring real a `SecureHTTPTransport`, provider de secreto aprobado, egress allowlist/mapping e idempotencia sandbox. Variable HTTP sola no es suficiente.
6. Manifest drift: comparar gates efectivos (`EXTERNAL_CONNECTOR_HTTP_ENABLED`, backend Blob, provider real, worker y cron ownership) y misma baseline/hash por proceso.
7. Backup PostgreSQL ejecutable en un entorno con `pg_dump`; ruta segura para almacenar/exportar artefactos y comando de restore PostgreSQL aislado verificable. El CLI actual no expone pg_restore ni Blob upload; Dockerfile no instala explícitamente los binarios PostgreSQL.
8. Graceful shutdown Worker (o demostrar con pruebas e invariantes que termination durante envío externo es recuperable sin duplicado). No aumentar workers/cron paralelos como compensación.

No se implementa ninguno en esta tarea. Los puntos 2–7 y Generic REST son bloqueantes del piloto; el helper cron puede solucionarse sin cambiar el modelo scheduler. El owner técnico debe priorizar/revisar el tamaño de 4/7/8 antes de prometer el primer rehearsal.

## 17. Acciones que requieren autorización humana

- Crear/cargar cualquier recurso Render, Neon, Vercel Blob o servicio de alerting; aceptar costes, región, retención, residencia, DPA, ownership y cuotas.
- Generar, consultar, copiar, rotar o revocar credenciales/secretos; inyectar claves Meta o Generic REST sandbox.
- Crear dominio/DNS, configurar webhook Meta, abrir egress/allowlists y permitir llamadas HTTP reales.
- Aplicar migrations/write queries, activar cron, habilitar una conexión, procesar pedidos sintéticos remotos o correr backup/restore.
- Seleccionar branch parent/datos iniciales, RPO/RTO/retención Neon, horario de alerta, on-call y quién aprueba la baseline.
- Cualquier push, deploy, tráfico de producción, datos reales, Sage u Odoo quedan expresamente fuera y no autorizados.

## 18. Blockers antes de provisioning/deploy

**Antes de crear recursos:** confirmar Render/Neon/Vercel workspaces, owner, región/residencia y budget; confirmar autorización contractual del Blob externo; definir retención/PITR Neon y responsable de alertas. No se detectaron credenciales/contexto autorizados en este encargo y no se buscaron ni usaron.

**Antes de desplegar servicio usable:** implementar el selector Blob y readiness fail-closed; corregir readiness HTTP; proporcionar invocador HTTP Cron o mecanismo aprobado; restringir módulos/jobs del perfil; habilitar Generic REST real con SecretProvider/egress seguro; fijar baseline manifest; cerrar migración/provisioning fresh tenant y restore PostgreSQL desde entorno limpio.

**Antes de GO PILOT:** Blob `write→restart→read`; PostgreSQL migrations/backup+restore; web/worker/cron independientes y restart; alerta externa demostrada; browser critical path; synthetic rehearsal y failed delivery recovery; aprobación humana de gates aplicables y acta GO/NO GO. Hasta entonces, decisión: **NO GO**.

## Fuentes externas consultadas

- Render: [Web Services/ports/health](https://render.com/docs/web-services), [Background Workers](https://render.com/docs/background-workers), [Cron Jobs](https://render.com/docs/cronjobs), [Persistent Disks](https://render.com/docs/disks), [Docker deploy commands](https://render.com/docs/deploys).
- Vercel: [Blob SDK y token externo](https://vercel.com/docs/vercel-blob/using-blob-sdk), [Blob security/private access](https://vercel.com/docs/vercel-blob/security).
- Neon: [Branching](https://neon.com/docs/introduction/branching), [Branch restore/PITR](https://neon.com/docs/introduction/branch-restore), [connection pooling](https://neon.com/docs/connect/connection-pooling).

## Addendum Block 13.2 — adaptaciones locales de runtime

Fecha: 2026-10-02. Este addendum sustituye las referencias anteriores a adaptaciones todavía pendientes en §§2, 4–10 y 16. Sólo se cambiaron código, tests y documentación local: no hubo provisioning, deploy, push, conexión a Neon/Blob ni petición al destino Generic REST.

| Gate | Estado de código | Evidencia/límite para el siguiente gate |
|---|---|---|
| Blob explícito/fail-closed | **READY FOR PROVISIONING** | `ATTACHMENT_STORAGE_BACKEND=vercel_blob`; token pasado explícitamente a `put/get/delete`; `BLOB_STORE_HOST` fija el hostname privado exacto para lecturas, escrituras y probe; lecturas requieren `company_id` y prefijo `tenants/<id>/attachments/`; filesystem sólo se usa si está elegido. Probe write/read/verify/delete cacheado 60 s por host/token. Sigue pendiente validar store/credencial real y `write → restart → read` entre procesos. |
| Readiness | **READY FOR PROVISIONING** | En `CATALOG_ONLY_PILOT`, el health check devuelve HTTP 503 para dependencia/config/drift no listos; `/health` y `/health/live` siguen siendo liveness sin dependencia DB. La readiness sin tenant autentica también todas las DB tenant activas y sus migraciones, con errores sanitizados. |
| Render Cron | **READY FOR PROVISIONING** | Ejecutar desde `/app/backend`: `python -m scripts.pilot_cron_invoker`. Lee `PILOT_CRON_ENDPOINT_URL`, `PILOT_CRON_ALLOWED_HOST`, `CRON_SECRET`, `PILOT_CRON_TIMEOUT_SECONDS`; sólo POST HTTPS a un hostname exacto `.onrender.com` coincidente con la allowlist, `/cron/jobs`, sin redirects, una llamada, timeout 1–120 s, exit no-cero HTTP/red y salida sin token. Un ciclo con jobs bloqueados retorna 503 y heartbeat unhealthy. Render schedule/alerta aún no probados. |
| Jobs Catalog-only | **READY FOR PROVISIONING** | `PILOT_PROFILE=CATALOG_ONLY_PILOT` y `PILOT_SCHEDULED_JOB_PROFILE=CATALOG_ONLY_PILOT` deben coincidir para activar la allowlist de `process_inbound_message`, `deliver_order`; perfil ausente/malformado/parcial en runtime no local no reclama jobs ni inicia provisioning. `process_order`/Email reprocess quedan fuera y su route da 404; ambos webhooks dejan media en revisión sin encolar downloader; Email worker e internal job worker se fuerzan off por perfil/estado inválido y sus switches forman parte del manifest/readiness. También excluye provisioning-worker y endpoints cron email/snapshots. Claim, stale recovery y locks siguen siendo los existentes. |
| Generic REST real/SecretProvider | **READY FOR PROVISIONING; REMOTE PROBE OPEN** | Se selecciona sólo con connection `ACTIVE`, `real_http_enabled`, `PILOT_GENERIC_REST_RUNTIME_ENABLED`, `EXTERNAL_CONNECTOR_HTTP_ENABLED`, perfil explícito `CATALOG_ONLY_PILOT` y referencia SecretProvider resoluble. Factory usa `GenericRESTConnector` → `HTTPGenericRESTTransport` → `SecureHTTPTransport`, DNS/SSRF, HTTPS, allowlist del host exacto, redirects bloqueados, límites y AuditLog existente. SecretProvider mínimo es `EnvironmentSecretProvider`; los secrets de Render deben nombrarse `ANCHI_SECRET_TENANT_<id>_CONNECTION_<id>_<NAME>`. Sin mock fallback. Como no hay operación health segura configurada, el test de conexión queda `BLOCKED` y no envía HTTP ni informa falso éxito; definir/validar el probe o probar una operación sintética autorizada en el rehearsal. Ninguna petición real se ha emitido; egress/destino sandbox y credencial requieren aprobación. |
| Backup/restore PostgreSQL | **READY FOR PROVISIONING** | `pilot_backup_restore.py` ofrece `restore-postgres`: sidecar/checksum obligatorio, identidad y host/puerto/database destino distintos, verificación de que el destino no tiene relaciones, rutinas, tipos de usuario ni schemas personalizados, `pg_restore --single-transaction`, y validación `SELECT 1`. Restore real e invariantes completas aún requieren una DB nueva autorizada. |
| Worker shutdown | **READY FOR PROVISIONING** | El proceso dedicado maneja SIGTERM/SIGINT entre ciclos; un job en curso termina por su ruta normal y los claims interrumpidos siguen sujetos a stale recovery/idempotencia. No se levantó un worker de Render. |
| Manifest/drift | **READY FOR PROVISIONING** | `storage_backend` y gates de Generic REST, HTTP externo, perfil de jobs y endpoint cron son críticos o quedan dentro de `runtime_gates`; no se incluyen valores secretos. El baseline final debe regenerarse/aprobarse para la configuración exacta antes de arrancar staging. |

Variables de provisioning (nombres, nunca valores en el repo): `PILOT_PROFILE=CATALOG_ONLY_PILOT`, `PILOT_ENABLED_MODULES=whatsapp_catalog,generic_rest`, `PILOT_DISABLED_MODULES=email_autodiscovery,audio,sage_real,odoo_real`, `PILOT_SCHEDULED_JOB_PROFILE=CATALOG_ONLY_PILOT`, `RUN_INTERNAL_EMAIL_WORKER=false`, `RUN_INTERNAL_JOB_WORKER=false` en Web, `ATTACHMENT_STORAGE_BACKEND=vercel_blob`, `BLOB_READ_WRITE_TOKEN`, `BLOB_STORE_HOST`, `PILOT_GENERIC_REST_RUNTIME_ENABLED`, `EXTERNAL_CONNECTOR_HTTP_ENABLED`, `PILOT_CRON_ENDPOINT_URL`, `PILOT_CRON_ALLOWED_HOST`, `PILOT_CRON_TIMEOUT_SECONDS`, `CRON_SECRET`, `PILOT_SECRET_PROVIDER=environment`, `ANCHI_SECRET_TENANT_<id>_CONNECTION_<id>_<NAME>`, `MASTER_DATABASE_URL`, `TENANT_DB_MODE=external`, `TENANT_DATABASE_URL`, `PILOT_CONFIGURATION_BASELINE_JSON`. Ambos perfiles deben coincidir; una configuración parcial con módulos Catalog-only queda en cero jobs y readiness 503. El Cron recibe únicamente URL, hostname allowlisted y secreto cron; no necesita token Blob. Web y Worker deben compartir el mismo host/store privado y scope de secreto; no crear el SecretProvider dentro de la DB.

El restore operativo a una base PostgreSQL nueva tiene esta forma, ejecutada por un operador autorizado con `pg_restore` disponible y sin usar el destino activo:

```bash
PYTHONPATH=backend python3 backend/scripts/pilot_backup_restore.py restore-postgres \
  --input <BACKUP.dump> \
  --database-url "$RESTORE_DATABASE_URL" \
  --target-identity <IDENTIDAD_RESTORE_DISTINTA>
```

`RESTORE_DATABASE_URL` no se guarda ni imprime. El destino debe estar vacío y pertenecer a la branch de recuperación autorizada. `SELECT 1` acredita conexión tras el restore, no reemplaza las comprobaciones de schema, filas, idempotencia, AuditLog e invariantes del runbook §14.

### Blockers antes de autorizar provisioning

- Aprobación humana de workspace/owner/coste/región/residencia y recursos Render, Neon y Vercel Blob; confirmación de proyecto/branch Neon vacía y store Blob privado dedicado. No se han creado recursos.
- Autorizar inyección de secretos en el entorno staging (sin compartir secretos reales en repo/chat), aprobar exactamente un Generic REST sandbox y su hostname/egress, y construir el baseline con los valores/gates finales. Los flags permanecen apagados por defecto y no se probó tráfico real.
- Decidir el probe Generic REST seguro (o la operación sintética aprobada) antes de considerar su conexión probada; ejecutar migraciones, fresh install, backup/restore e invariantes sobre la branch aislada; demostrar restart-read del Blob Web↔Worker; desplegar Worker/Cron separados; configurar y recibir una alerta sintética; browser critical path; synthetic pilot y recovery.
- Mantener RBAC/retención según sus aprobaciones humanas existentes y realizar la revisión final de código. Esto no es `GO PILOT` ni autorización automática para provisionar.

**Estado Block 13.2:** código local **READY FOR PROVISIONING** tras tests/review; validación proveedor/browser/alertas y rehearsal siguen **OPEN**. Decisión de go-live: **NO GO** hasta cerrar esos gates.

### Export y aprobación del baseline completo

Desde `/app/backend` en el runtime staging (o desde el mismo commit y con acceso únicamente a las URLs de Neon staging), generar el manifiesto del tenant sintético seleccionado explícitamente:

```bash
python -m scripts.pilot_configuration_manifest --company-id <PILOT_COMPANY_ID>
```

El comando consulta master y la DB tenant, exige el perfil Catalog-only válido, módulos permitidos exactos, `TENANT_DB_MODE=external`, commit SHA completo, Blob configurado explícitamente, Cron protegido configurado, SecretProvider `environment`, workers internos apagados y ambos esquemas `current`; no escribe en esas bases y no ejecuta el probe Blob. Rechaza tenant/company inexistente o inactivo. Utiliza `build_tenant_configuration_manifest`, compartido con `/health/ready`, y `PilotConfigurationManifest` para producir JSON ordenado con el mismo `hash` de drift/readiness. Los fallos de drivers se resumen por tipo y nunca incluyen URL, credenciales ni traceback.

El flujo de aprobación es: desplegar el commit aprobado; configurar recursos y gates; ejecutar el comando para el ID sintético; revisar y aprobar el JSON completo como artefacto de configuración; cargar ese JSON sin modificar en `PILOT_CONFIGURATION_BASELINE_JSON`; reiniciar con esa configuración y comprobar `configuration_drift.status == "MATCH"` y el mismo `configuration_manifest_hash` en `/health/ready` con contexto autenticado del tenant/company seleccionado. La llamada sin contexto tenant calcula el manifest de servicio y no equivale a comparar el baseline tenant-specific. Repetir la exportación si cambia commit, schema, conexiones, rutas, gates, storage o políticas. No se añade endpoint HTTP para exportar el manifest ni opción para imprimir secretos.

El JSON incluye el identificador de tenant, commit, versiones de schema, configuración de módulos/conectores/rutas/gates, fingerprints unidireccionales y hash. No incluye URLs DB, valores de secretos, credenciales resueltas ni nombre/email del tenant. Los fingerprints no son credenciales; representan una huella de la configuración no secreta tras la redacción por clave.
