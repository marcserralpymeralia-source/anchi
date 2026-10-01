# Block 13 — Final Pilot Gate

Fecha: 2026-10-01  
Base: `ce99718 feat(pilot): close infrastructure and operational readiness`  
Worktree: `modular-order-hub`  
Alcance: validación de gate; sin producción, push, deploy ni recursos cloud  
Decisión final: **NO GO**

## A. Pilot profile

El perfil formal es [catalog-only-pilot-profile.md](./catalog-only-pilot-profile.md). Incluye WhatsApp Catalog → Canonical Order → Workbench → Generic REST aprobado → OrderDelivery/Audit y excluye email/autodiscovery, audio, Sage real, Odoo real, adjuntos y destinos no aprobados.

Estado: **FORMALIZADO / ACTIVACIÓN OPEN**. El flujo queda validado sintéticamente, pero la definición actual de `generic_rest` es `runtime_enabled=false`, `mock_only=true`; no existe un destino REST real autorizable en este worktree.

## B. Optional-module isolation

Los cuatro fallos residuales se reprodujeron individualmente:

| Test | Resultado | Módulo | Efecto sobre el piloto |
|---|---|---|---|
| `test_common_fallback_does_not_treat_bare_domain_as_mail_server` | FAIL | email autodiscovery | Excluido |
| `test_known_provider_returns_verified_imap_without_password` | FAIL | email autodiscovery | Excluido |
| `test_pop3_only_is_reported_but_not_marked_usable_by_anchi` | FAIL | email autodiscovery | Excluido |
| `test_audio_download_waits_for_transcription_before_pipeline` | FAIL | WhatsApp audio/transcripción | Excluido |

La suite pilot-critical que cubre startup/configuración, readiness, Catalog, Order, Workbench, Generic REST mock, delivery, audit, RBAC, migraciones, worker y recovery terminó correctamente. No se habilitó ninguno de los módulos residuales.

## C. Pilot-critical suite

Comando ejecutado:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend \
  python3 -m unittest \
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
  backend.tests.test_regression_flows \
  backend.tests.test_pilot_closure
```

Resultado: **207 tests, 0 failures, 0 errors, OK**.

## D. Runtime detectado

La topología prevista en el repositorio es híbrida:

| Componente | Runtime previsto | Evidencia | Estado Block 13 |
|---|---|---|---|
| Web | Docker/Procfile o Vercel serverless | `Dockerfile`, `Procfile`, `vercel.json` | Definido, no desplegado aquí |
| Worker | proceso Docker/Procfile dedicado | `RUN_INTERNAL_JOB_WORKER=false` en web y `true` en worker | Definido, no activo aquí |
| Scheduler | Vercel Cron para `/cron/jobs`, `/cron/email-sync`, `/cron/platform-snapshots` | `vercel.json` | Declarado, ejecución del proveedor no demostrada |
| PostgreSQL | externo en demo/Vercel; Compose local sólo rehearsal | `.env.example`, Compose | Sintético validado; final no disponible |
| Persistent storage | Vercel Blob sólo con `BLOB_READ_WRITE_TOKEN`; filesystem fuera de Vercel | `attachment_storage.py` | Final no configurado |
| SecretProvider | `EnvironmentSecretProvider`/mapping; referencias `secret://...` | `connectors/security.py` | Gestionado real no disponible |
| Alertas | alertas de dominio tenant; sin monitor externo de heartbeat/cron/storage/backup | `alerts/`, `operational.py` | Gate OPEN |

Los contenedores que estaban activos pertenecen a otros stacks; no fueron usados ni modificados. No existe `.vercel` link ni `render.yaml` en el worktree.

## E. PostgreSQL

PASS **sintético heredado de Block 12.1**: instalación fresca, migraciones master/tenant, segunda ejecución no-op, backup/restore e invariantes. No existe una instancia final aislada y autorizada para repetirlo en Block 13; no se inventa evidencia de proveedor.

## F. Persistent storage

OPEN bloqueante. Los tests locales cubren readiness, aislamiento y supervivencia entre procesos con `TEMP_STORAGE_DIR`, pero no hay volumen/Blob final ni prueba autorizada `write → restart → read` en el runtime previsto. El filesystem temporal no es storage persistente de piloto.

## G. Web

PASS sintético heredado: proceso Uvicorn separado, `/health/live` y `/health/ready` funcionaron contra PostgreSQL sintético. El runtime final web no está desplegado ni enlazado en este worktree.

## H. Worker

PASS sintético heredado: worker dedicado, heartbeat, claim/retry/stale y recuperación tras reinicio. No se demostró monitor externo de worker muerto o backlog envejecido.

## I. Scheduler/Cron

PASS local condicionado heredado: `/cron/jobs` exige `CRON_SECRET` y dos ejecuciones no duplicaron trabajo. OPEN para cron real del proveedor, propietario único, latencia, errores 401/403/timeout y snapshots programados.

## J. Alerts

OPEN. Existen alertas de negocio y estado durable de heartbeat, pero no hay integración externa autorizada para alertar worker muerto, scheduler stale, storage no disponible, backup/checksum/restore fallido o backlog envejecido.

## K. Browser

OPEN. El smoke nativo local de Block 12.1 cubrió login, tenant, pedidos, Workbench y failed delivery. No existe runtime final enlazado ni browser acceptance del proveedor; el diálogo de offboarding quedó bloqueado por el driver al confirmar y no se declara E2E PASS.

## L. Backup/restore

PASS sintético heredado: `pg_dump`/`pg_restore` master+tenant, checksums e invariantes. OPEN para backup gestionado, cifrado, retención, RPO/RTO, alertas y restore en el proveedor final.

## M. Failed-delivery recovery

PASS sintético: la entrega fallida apareció en UI, el retry manual fue auditado y el worker procesó el job hasta clasificar `connector_not_registered` para el conector no habilitado. OPEN para repetir con un destino REST aprobado y final.

## N. Offboarding

PASS técnico sintético: offboarding idempotente, compañía/membership/canales inactivos, jobs/deliveries pendientes pausados, histórico y audit preservados, referencias externas de secreto conservadas para revocación. La confirmación browser final no se declara PASS por la limitación del driver.

## O. RBAC: APPROVED

**APPROVED** — Pilot Owner: Marc Serral, Decision: APPROVED, Date: 2026-10-01.

La revisión técnica confirma la separación propuesta: Operador/Supervisor no ejecutan offboarding; Administrador opera el tenant; Superadmin gestiona tenants y offboarding. Queda abierta una discrepancia: `Administrador` hereda `view_technical_logs`, mientras la cadena de permisos de plataforma `Superadmin` no contiene esa clave; aunque las rutas de plataforma usan `require_superadmin`, la separación del permiso técnico no es limpia. No se cambia código en este encargo.

## P. Retention: APPROVED

**APPROVED** — Pilot Owner: Marc Serral, Decision: APPROVED, Date: 2026-10-01.

La política aprobada define plazos operativos, legal hold, safe default y offboarding sin borrado automático. El enforcement automático, storage durable, backups gestionados y revocación externa no están validados; el gate técnico permanece abierto.

## Q. P0/P1 abiertos

No se ha demostrado un P0 en esta revisión. Permanecen P1 abiertos:

- storage durable del runtime final y readiness asociado;
- Generic REST real, SecretProvider gestionado y destino aprobado;
- cron del proveedor y alertas externas;
- backup/restore gestionado, RPO/RTO y retención;
- browser acceptance del runtime final;
- discrepancia técnica de `view_technical_logs` y enforcement de retención;
- manifest/configuration drift del entorno que se vaya a entregar.

Email autodiscovery y audio/transcripción quedan fuera del contrato, pero sus fallos permanecen documentados.

## R. Decisión final

**NO GO**

No se ejecutan deploy, push, cambios remotos, uso de credenciales, activación de HTTP externo ni tráfico real. El gate sólo puede reconsiderarse cuando exista un runtime final aislado y autorizado y se cierren los P1 técnicos restantes, incluida la discrepancia RBAC y el enforcement de retention.
