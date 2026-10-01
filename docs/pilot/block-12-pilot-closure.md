# Block 12 — Pilot Closure

Fecha: 2026-10-01  
Worktree: `modular-order-hub`  
Commit base de Block 10/11: `3df6d8b`  
Alcance: cierre técnico y operativo; no autoriza producción ni tráfico real.

## 1. Objetivo

Convertir la evidencia de Blocks 10/11 en un cierre reproducible de infraestructura, operación y aceptación, dejando separados los hechos demostrados offline de las validaciones que sólo pueden ejecutarse en el runtime final.

## 2. Alcance ejecutado

- Backup/restore aislado para SQLite y adaptador estándar PostgreSQL `pg_dump`/`pg_restore`.
- Metadata de backup con timestamp, entorno, identidad, motor, checksum y versión/checksum de esquema cuando existe.
- Heartbeats durables de worker y scheduler, estado operativo en readiness y detección de stale.
- Persistencia de adjunto tras reinicio de proceso y prueba visible de fallo/recuperación de storage.
- Offboarding autorizado y auditado: pausa entradas/salidas y trabajo pendiente, desactiva conexiones/rutas, conserva historial e identifica referencias de secretos para revocación externa.
- Guardas de enqueue para impedir nuevos jobs/deliveries de una compañía pausada u offboarded.
- Aislamiento de storage de previews entre tests/procesos.
- Suite específica de cierre y correcciones de determinismo de tests.

## 3. Alcance no ejecutado

- No se ejecutó contra PostgreSQL gestionado ni contra Blob/objeto persistente del proveedor final.
- No se demostró ejecución real de cron del proveedor ni alerta externa.
- No se completó una rehearsal con procesos web, worker y scheduler separados conectados a un entorno de staging.
- No se usaron credenciales ni datos de producción.
- Email autodiscovery/backfill, WhatsApp audio/transcripción y conectores externos permanecen fuera del contrato del piloto.

## 4. Infraestructura y operación

### Topología aprobada para rehearsal

```text
browser
  -> web/API (stateless)
       -> master PostgreSQL + tenant PostgreSQL
       -> private persistent object storage
cron provider --secret--> /cron/jobs, /cron/email-sync, /cron/platform-snapshots
worker service (dedicated, one owner) -> tenant jobs/outbox
secret manager -> DB/encryption/connector references
```

Localmente, SQLite y filesystem temporal sólo representan el contrato offline. El worker debe ser un proceso separado del web; el scheduler debe tener frecuencia, owner y locking/idempotencia definidos por entorno.

### Inventario de schedulers

| Job | Clasificación | Owner/runtime | Frecuencia | Lock/idempotencia | Scope | Fallo y última ejecución |
|---|---|---|---|---|---|---|
| `jobs` | REQUIRED | worker dedicado + cron provider | según `PILOT_REQUIRED_SCHEDULER_JOBS`/provider | claim transaccional, stale recovery, dedupe | todos los tenants activos | heartbeat `scheduler/jobs`; alertar si error o >5 min stale |
| `email-sync` | OPTIONAL | cron provider | sólo si email se habilita | checkpoint/UID + dedupe | tenants con canal email activo | heartbeat propio; fuera del piloto Catalog-only |
| `platform-snapshots` | OPTIONAL | cron provider | según ventana de backup | identidad/artefacto/checksum | master + tenants aprobados | heartbeat propio; restore requerido antes de GO |
| scheduler ad-hoc duplicado | DISABLED | ninguno | no permitido | — | — | detectar por owner/heartbeat y no desplegar segundo owner |

Estado operativo expuesto por readiness: `web`, `database`, `storage`, `worker`, `scheduler` y contador de entregas fallidas. El mínimo de alerta es: storage no escribible, base no disponible, worker sin heartbeat >5 minutos, job requerido stale/error, backlog envejecido, fallo de backup/checksum/restore o deliveries fallidas sin recuperación.

## 5. Backup, restore y storage

El procedimiento completo está en [pilot-backup-restore-runbook.md](./pilot-backup-restore-runbook.md). La prueba offline pasó backup, checksum, restore, invariantes de master/tenant y retry sin duplicado.

Clasificación de artefactos:

| Artefacto | Política | Fallo esperado |
|---|---|---|
| adjuntos/PDF y documentos extraídos | persistentes, privados, por tenant | rechazar/registrar, readiness no OK; no afirmar éxito |
| uploads/import previews | temporales con TTL, nunca única copia de negocio | limpiar sólo tras TTL y con dry-run |
| exports | persistentes hasta retención aprobada, con referencia auditable | delivery queda recuperable/fallido |
| temp artifacts/probes | efímeros, limpiables | no afectan datos de negocio |
| backups | fuera del runtime, cifrados, con sidecar y retención | conservar último válido y alertar |

La prueba `test_persistent_storage_survives_process_restart` guarda un objeto en un proceso y lo lee desde otro. La equivalencia contra el volumen/Blob final queda abierta.

## 6. Offboarding y retención

El endpoint superadmin `POST /superadmin/companies/{company_id}/offboard` exige actor autorizado, razón y CSRF; la acción es idempotente y auditada. Desactiva input channels, integration connections y routes; pausa jobs/deliveries queued/retrying; impide nuevos jobs/deliveries; conserva orders, mensajes, entregas, intentos, identidades externas y audit logs. Devuelve las referencias `secret_provider_ref` para revocarlas en el gestor externo; no muestra ni borra el secreto.

La retención técnica mínima del piloto es conservar historial operativo y backups durante la ventana aprobada. La limpieza de previews, temporales y exports debe tener política, TTL, dry-run y aprobación; no se implementa borrado destructivo de tenant como parte de este bloque. La matriz legal de PII, exportación, anonimización y plazo final sigue siendo una aprobación externa requerida antes de datos reales.

## 7. Acceptance determinista

Suite de cierre:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend \
  python3 -m unittest -v backend/tests/test_pilot_closure.py
```

Suite dirigida de regresión: 35/35 OK tras aislar `TEMP_STORAGE_DIR` y resetear `APP_ENV`/settings entre módulos. Suite offline completa post-fix: `Ran 681 tests in 248.800s`, `FAILED (failures=4, skipped=1)`; los 4 fallos son autodiscovery de email (3) y audio/transcripción (1), fuera del contrato Catalog-only. Las pruebas de red real son opt-in y no deben ejecutarse como parte del gate offline.

## 8. Browser acceptance y rehearsal

El smoke browser sintético previo cubrió login, tenant, pedidos, workbench y entregas fallidas. Para Block 12 se añade el criterio de RBAC/offboarding: operador/supervisor no puede ejecutar offboarding; superadmin debe confirmar razón y la compañía offboarded no puede iniciar nuevos trabajos. `agent-browser` no está instalado en este entorno; la evidencia browser local se obtuvo mediante navegador nativo, por lo que falta repetir en el navegador/proveedor final.

La rehearsal de go-live debe ejecutar, con datos sintéticos y un destino controlado: pedido válido, duplicado, revisión manual, exportación, fallo permanente, retry manual, parada/reinicio de worker, snapshot/restore y offboarding. Debe guardar IDs de inbound/order/delivery/job/attempt/audit, health snapshots y resultado de alertas.

## 9. Gap register actualizado

- RESUELTO en código/tests offline: G-03 storage de tests, G-19, G-21, G-23; G-10 sólo para SQLite aislado; G-11/G-12 sólo en estado/heartbeat local.
- RESUELTO parcialmente: G-01/G-02 storage configurable y readiness; G-08 recovery manual; G-16 offboarding técnico y retención dry-run; G-17 guardas RBAC/offboarding.
- ABIERTO y bloqueante: G-01/G-02 runtime persistente, G-08 staging/destino real, G-09 migración real, G-10 PostgreSQL restore/política gestionada, G-11/G-12 alertas y runtime separado, G-13 manifest/drift, G-16 aprobación de PII/retención, G-17 matriz firmada, G-18 browser final.
- ABIERTO y fuera del contrato Catalog-only: email/autodiscovery/backfill, audio/transcripción y conectores no incluidos.

## 10. GO / NO GO final

**NO GO**

Razón: el cierre offline demuestra invariantes y recuperación sintética, pero no hay evidencia del runtime final persistente, PostgreSQL real, Blob/volumen, proveedor cron/alertas, rehearsal separada web-worker-scheduler, browser final ni aprobación externa de retención/RBAC. No se debe abrir tráfico real hasta cerrar esos gates.

## 11. Archivos modificados y creados

La lista exacta se obtiene con `git status --short`; los cambios de Block 12 están separados del baseline `3df6d8b`. El commit sugerido para esta entrega es:

```text
feat(pilot): close infrastructure and operational readiness
```

## 12. Riesgos restantes

El principal riesgo es confundir un rehearsal SQLite/filesystem con la garantía del proveedor final. También queda pendiente revocar referencias de credenciales en el sistema externo y firmar la política de retención antes de aceptar datos reales.
