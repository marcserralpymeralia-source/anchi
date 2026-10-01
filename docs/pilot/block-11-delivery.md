# Entrega Block 11 — Infraestructura, Operación y Acceptance

Fecha: 2026-10-01
Ámbito: `modular-order-hub`
Decisión final: **NO GO para tráfico real**

## Resultado ejecutivo

Block 11 deja implementada la base operativa que faltaba para storage readiness y recuperación manual de entregas. También ejecuta una instalación sintética desde cero, una navegación browser contra el servidor local y la simulación de volumen ya preparada en Block 10.

La decisión sigue siendo **NO GO** porque no se ha demostrado backup/restore en el proveedor final, ejecución real de cron/alertas, storage persistente tras reinicio, ni offboarding/retención aprobado. La aceptación local no sustituye esas pruebas.

## 11A — Infraestructura

| Área | Evidencia | Estado |
|---|---|---|
| Storage | `check_storage_readiness()` prueba write/read/delete en filesystem local y rechaza Vercel sin `BLOB_READ_WRITE_TOKEN`; tests de storage OK. | Implementado; staging pendiente |
| Migrations | `test_schema_migrations.py`: 24/24; cubre dry-run, checksum, upgrade, preservación de datos y segunda ejecución no-op. | Verde en copias SQLite |
| Fresh install | Se crearon master y tenant SQLite sintéticos vacíos, se sembraron compañía/usuario y arrancó Uvicorn con configuración explícita. | Rehearsal local OK |
| Upgrade | Las pruebas de upgrade preservan conteos/estado y reparan esquemas legacy. | Verde en tests; PostgreSQL pendiente |
| Backup/restore | No se ejecutó `pg_dump/pg_restore` ni restore de objetos persistentes en el proveedor final. | **Bloqueante** |
| Worker | Claim, scope por tenant, stale recovery, retry e idempotencia cubiertos por `test_worker_tenant_scope.py` y `test_jobs_reliability.py`. | Verde en lógica |
| Cron | Las rutas exigen `CRON_SECRET`; no hay evidencia de invocación por el proveedor ni alerta de fallo/última ejecución. | **Bloqueante** |
| Readiness | `/health/ready` incluye storage, schema master/tenant y estado de workers; la prueba browser directa de JSON fue bloqueada por el cliente, por lo que se validó por tests y servidor. | Implementado; snapshot de staging pendiente |

## 11B — Operación

| Área | Evidencia | Estado |
|---|---|---|
| Failed deliveries | Nueva página `/orders/deliveries/failed`, enlazada desde monitor de jobs, con último error, intentos, tipo e idempotency key. | Implementado |
| Manual recovery | `POST /orders/deliveries/{id}/retry`, limitado a Administrador/Supervisor, tenant-scoped, auditado y sin crear otra delivery. Test específico OK. | Implementado; staging pendiente |
| RBAC | Dependencia de rol aplicada al retry; tests de aislamiento, superadmin y último owner suspendible cubiertos. | Verde condicionado |
| Offboarding | Suspender revoca sesiones y protege al último owner; no existe todavía política ejecutable de retención, borrado/anonimización de blobs, logs y backups. | **Bloqueante** |
| Operator tooling | Monitor de jobs enlaza a recuperación; existe trazabilidad por delivery, job, intento y audit. | Implementado; timeline unificado pendiente |

## 11C — Acceptance

### Browser E2E local

Se usó el browser nativo del entorno porque el binario `agent-browser` no estaba instalado. Sobre el servidor sintético se verificó:

1. login con usuario de tenant;
2. redirección a `/setup/company` y entrada en pedidos;
3. navegación a `/orders/deliveries/failed` con estado vacío visible;
4. creación de pedido de prueba;
5. carga de `/orders/1` con workbench, adjunto PDF mock y acciones de guardar/validar;
6. carga limpia posterior del detalle en una segunda pestaña.

La primera observación registró un `MutationObserver` del entorno browser; la carga limpia posterior no lo reprodujo y no hubo error de consola persistente. Debe repetirse con el runner y navegador finales.

### Fresh deployment rehearsal

El rehearsal utilizó únicamente datos sintéticos en `/private/tmp/anchi-block11-QYJT4C`, con master DB, tenant DB, storage temporal y credenciales de prueba. El arranque terminó correctamente y dejó workers internos desactivados, como corresponde a un contrato con servicios dedicados. No se tocó producción ni se usaron credenciales reales.

### Go-live simulation

- Simulación sintética previa: 100 pedidos → 100 deliveries → 100 jobs.
- Reliability: retries, reconciliación, stale recovery, claim por tenant e idempotencia cubiertos por tests.
- Operación: retry manual de delivery fallida devuelve `delivery_id`, `order_id`, estado `queued`, intentos y clave de idempotencia.
- Browser: flujo de login, workbench y navegación de recuperación completado localmente.

No se simuló un destino PostgreSQL/ERP real, un restore gestionado, una caída del proveedor de cron ni la conservación de adjuntos después de reiniciar el runtime.

## Validaciones ejecutadas

| Comando/evidencia | Resultado |
|---|---|
| `test_import_storage.py test_order_delivery_reliability.py` | 21/21 OK |
| `test_observability.py` | 9/9 OK |
| `test_schema_migrations.py` | 24/24 OK |
| Worker/jobs/tenant/superadmin/account lifecycle | 67 tests; 62 OK, 4 fallos de harness de sesión, 1 error de storage local no escribible |
| Full suite previo | 673 tests; 10 fallos, 2 errores, 1 omitido; clasificado en `pilot-gap-register.md` |
| Syntax validation | Parseo AST en memoria de 6 módulos modificados: OK; `py_compile` no pudo crear `__pycache__` por permisos del worktree |

Los 4 fallos de multitenancy del bloque combinado y el error de preview se reproducen por el entorno local no escribible/estado global del harness; el registro conserva la clasificación y no se presentan como verdes.

## GO / NO GO final

**NO GO** para usuarios o datos reales.

Para reabrir el gate se necesitan, como mínimo:

- backup y restore real master + tenant + objetos, con RPO/RTO;
- storage persistente probado tras reinicio y readiness snapshot;
- cron/worker gestionados con heartbeat, backlog y alertas;
- matriz RBAC y offboarding/retención aprobados;
- browser smoke en el runtime y navegador finales;
- suite de release determinista o excepción formal firmada para los casos restantes.

## Archivos cambiados en Block 11

- `backend/app/core/storage.py`
- `backend/app/health/routes.py`
- `backend/app/orders/routes.py`
- `backend/app/templates/jobs/monitor.html`
- `backend/app/templates/orders/failed_deliveries.html`
- `backend/tests/test_import_storage.py`
- `backend/tests/test_observability.py`
- `backend/tests/test_order_delivery_reliability.py`
- `docs/pilot/pilot-gap-register.md`
- `docs/pilot/block-11-delivery.md`

Commit sugerido: `feat(pilot): add readiness and failed-delivery recovery`
