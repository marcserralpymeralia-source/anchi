# Block 12.1 — Validación final de runtime, operación y aceptación

Fecha: 2026-10-01  
Worktree: `modular-order-hub`  
Ámbito: ANCHI-GEMAVI, sólo datos sintéticos, sin producción  
Decisión final: **NO GO**

## 1. Objetivo

Cerrar la validación final de infraestructura y operación para un primer piloto Catalog-only y decidir si existe evidencia suficiente para abrir tráfico de piloto. Esta acta no autoriza producción ni sustituye la aprobación humana de seguridad, legal u operaciones.

## 2. Contrato de piloto evaluado

Incluido: WhatsApp Catalog estructurado, Canonical Order, Workbench, Integration Registry, conexión Generic REST aprobada, OrderDelivery, Audit, Onboarding, entregas fallidas y recovery manual.

Excluido explícitamente: email, autodiscovery/backfill, audio/transcripción, Sage, Odoo, adjuntos como capacidad contractual y conectores externos no aprobados. `EXTERNAL_CONNECTOR_HTTP_ENABLED=false` en la rehearsal.

## 3. Topología inspeccionada

| Componente | Evidencia local | Resultado |
|---|---|---|
| PostgreSQL | Compose aislado `anchi-block12`, `postgres:16`, host `55432`, volumen nombrado | PASS sintético |
| Master DB | `migrate-master`, versión `2026.10.01.1`, checksum `89f2d9...e975fd` | PASS |
| Tenant DB | `migrate-tenant`, versión `2026.09.30.4`, checksum `00c44c...d60a516` | PASS |
| Web | proceso Uvicorn separado en `127.0.0.1:18000` | PASS |
| Worker | proceso `app.workers.jobs_worker` separado | PASS |
| Scheduler | `/cron/jobs` protegido con `CRON_SECRET`; cron de proveedor descrito en `vercel.json` | PASS local / proveedor pendiente |
| Object/blob storage | sólo filesystem local en la rehearsal; Blob requiere credencial/configuración del proveedor | OPEN bloqueante |

No se detuvieron ni modificaron otros stacks Docker locales.

## 4. Fresh install, upgrade y migraciones

Se destruyó y recreó únicamente el proyecto Compose sintético, se creó una base tenant vacía y se ejecutó el flujo completo:

1. `migrate-master`: creó el esquema y terminó `current`.
2. `provision-demo`: creó compañía, tenant, usuario y membership; se corrigió el wrapper de provisioning para aceptar credenciales sintéticas del admin.
3. `migrate-tenant`: terminó `current`, sin acciones pendientes.
4. `health` y `seed-demo`: tenant operativo y 20 clientes, 20 productos, 20 pedidos e importación sintética.
5. Repetición de migración: no-op con versión/checksum actuales.

Resultado: PASS para PostgreSQL sintético fresco y upgrade/no-op. Falta repetir contra el proveedor final y guardar su snapshot de migración.

## 5. Backup y restore PostgreSQL

Se ejecutó `pg_dump --format=custom --no-owner` para master y tenant y se restauró cada dump en una base nueva dentro del mismo PostgreSQL sintético.

| Dump | SHA-256 | Verificación restaurada |
|---|---|---|
| `anchi_master_block121.dump` | `849afb4d586f98976d6d7ec128a7b4b28463f5aa1aabd76e509520c8e3b35173` | 1 company, migración presente, compañía `offboarded` |
| `anchi_tenant_block121.dump` | `053f1e546334939f43a9ff3f8179b0c18708b075d955b68b96ec1d9e838e1110` | 20 orders, 1 delivery, 1 job, audit `tenant.offboard`, 4/4 canales inactivos |

Resultado: PASS sintético para backup, restore e invariantes de base. OPEN para política gestionada, cifrado, retención, RPO/RTO y restauración en el proveedor final.

## 6. Storage, reinicio y aislamiento

- Readiness ejecutó `write/read/delete` y respondió `storage_ready=true` con backend `filesystem`.
- Los tests de aislamiento y de fallo de storage pasan en la suite crítica.
- La rehearsal no demuestra persistencia de un objeto de negocio después de reiniciar el runtime porque usa filesystem local temporal.
- El contrato de adjuntos/blob privado, cuota, cifrado, retención y recuperación sigue OPEN; por ello no se puede aceptar media real.

## 7. Web, worker y scheduler

- Web y worker arrancaron como procesos separados y el worker publicó heartbeat con owner distinto.
- Se detuvieron ambos procesos y se arrancaron de nuevo; web volvió a `/health/ready` `200` y PostgreSQL conservó los datos.
- `/cron/jobs` se ejecutó dos veces con el secreto sintético: ambas respuestas fueron `200`, `tenants=0`, `processed=0`, sin duplicación. La segunda ejecución no creó trabajo adicional.
- Antes del offboarding, readiness mostró worker healthy y scheduler healthy con heartbeat requerido `jobs`. Después del offboarding, no había tenants activos y el scheduler quedó stale/no aplicable; esto se conserva como evidencia de alerta, no como PASS de proveedor.
- Falta configurar y probar la alerta externa ante worker muerto, backlog envejecido, cron stale, error de backup y storage no disponible.

## 8. Operación y offboarding

La rehearsal creó una entrega fallida sintética, la mostró en `/orders/deliveries/failed` y el botón de retry la volvió a encolar. El worker la procesó y dejó el error permanente visible: `connector_not_registered` para `generic_rest`, que está fuera de la configuración activa.

Offboarding técnico verificado en master/tenant: compañía inactiva con estado `offboarded`, 4/4 canales inactivos, memberships desactivadas, rastro `company.offboard`/`tenant.offboard`, histórico de pedidos/entrega/job conservado y nuevos jobs/deliveries bloqueados. El servicio es idempotente.

El navegador nativo mostró la pantalla, el botón y el diálogo de confirmación. El driver quedó bloqueado al confirmar el diálogo; por rigor, el efecto no se declara como E2E browser PASS y se acredita con la ejecución de servicio/ORM en el entorno sintético.

## 9. Suite crítica del piloto

Comando:

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

Full suite local posterior a los fixes: **681 tests, 4 failures, 1 skipped**. Los cuatro fallos son reproducibles y clasificados: tres de `test_settings_email_autoconfig` y uno de audio/transcripción; todos están fuera del contrato Catalog-only y permanecen visibles en `pilot-gap-register.md`.

## 10. Reproducción y clasificación de fallos conocidos

| Caso | Resultado | Clasificación | Decisión |
|---|---|---|---|
| Email autodiscovery bare domain | Reproducido | candidato/probe DNS | Fuera del piloto; configuración manual |
| Email autodiscovery proveedor conocido | Reproducido | probe/configuración externa | Fuera del piloto |
| Email POP3-only | Reproducido | proveedor no usable por contrato | Fuera del piloto |
| WhatsApp audio/transcripción | Reproducido | media/transcripción | Fuera del piloto; no anunciar soporte |

## 11. Aprobaciones humanas

- [Aprobación de retención y offboarding](./pilot-retention-approval.md): **PENDING HUMAN APPROVAL**.
- [Aprobación de RBAC](./pilot-rbac-approval.md): **PENDING HUMAN APPROVAL**.

No se han inventado plazos de retención, RPO/RTO, MFA ni reglas legales. Esos valores deben completarse y firmarse antes de aceptar datos reales.

## 12. Go-live simulation y criterios de decisión

La rehearsal cubrió instalación fresca, seed sintético, login/tenant/workbench/pedidos, entrega fallida, retry manual, worker separado, cron protegido, backup/restore, offboarding y trazas de audit/correlation/job. No cubrió el proveedor final, Blob/volumen persistente, alertas externas ni confirmación browser final en el navegador soportado.

| Gate | Estado | Consecuencia |
|---|---|---|
| Suite crítica | PASS | No bloquea |
| PostgreSQL fresh/upgrade/restore sintético | PASS | Repetir en proveedor final |
| Storage durable final | OPEN | Bloquea |
| Worker/web separados | PASS sintético | Alerta externa pendiente |
| Cron del proveedor y alertas | OPEN | Bloquea |
| RBAC/retención aprobados | PENDING HUMAN APPROVAL | Bloquea |
| Browser/proveedor final | OPEN | Bloquea |

## 13. Gap register

El detalle actualizado queda en [pilot-gap-register.md](./pilot-gap-register.md). Block 12.1 cierra la evidencia local de PostgreSQL, migraciones, backup/restore sintético, health, worker separado, retry manual y offboarding técnico. Permanecen bloqueantes G-01/G-02, G-08/G-09/G-10 en proveedor final, G-11/G-12 alertas, G-13 drift, G-16 aprobación de retención y G-17 aprobación RBAC/G-18 browser final.

## 14. GO / NO GO final

**NO GO**

No se abre tráfico real ni se usa la etiqueta `GO PRODUCTION`. El siguiente paso autorizado es cerrar los gates OPEN/PENDING en el runtime final, repetir esta acta con evidencia del proveedor y obtener las aprobaciones humanas.

## 15. Commit sugerido

```text
docs(pilot): record final runtime acceptance
```
