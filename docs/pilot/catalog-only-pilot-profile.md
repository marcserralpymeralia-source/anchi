# Perfil operativo `Catalog-only` del piloto

Fecha: 2026-10-01  
Estado: **FORMALIZADO / ACTIVACIÓN OPEN**  
Ámbito: ANCHI-GEMAVI, únicamente datos sintéticos durante la validación

## Contrato incluido

```text
WhatsApp Catalog
  -> inbound/catalog event
  -> Canonical Order
  -> Workbench/revisión humana
  -> conexión Generic REST aprobada
  -> OrderDelivery/idempotencia/recovery
  -> Audit/correlation trace
```

El tenant debe tener catálogo, clientes/productos mapeados, una ruta de pedido y una única conexión de destino aprobada. La confirmación y exportación deben ser trazables por actor, pedido, delivery, job, intento e idempotency key.

## Módulos excluidos

- email, IMAP, autodiscovery y backfill;
- audio y transcripción;
- Sage real;
- Odoo real;
- adjuntos como capacidad contractual;
- conexiones y destinos no aprobados;
- tráfico HTTP externo sin contrato, SecretProvider y allowlist aprobados.

Los cuatro fallos residuales conocidos pertenecen sólo a email autodiscovery y audio/transcripción. Se mantienen visibles y no se convierten en `skip` silencioso.

## Perfil de configuración esperado

| Control | Valor/criterio |
|---|---|
| `APP_ENV` | `demo`/staging durante rehearsal; `production` sólo tras cerrar todos los gates |
| `TENANT_DB_MODE` | `external` |
| `MASTER_DATABASE_URL` / tenant DB | PostgreSQL gestionado y persistente |
| workers internos del web | desactivados; worker dedicado |
| `IMAP_SYNC_ENABLED` / `EMAIL_AUTO_SYNC_ENABLED` | `false` |
| audio | no anunciado ni incluido en el contrato de entrada |
| `EXTERNAL_CONNECTOR_HTTP_ENABLED` | `false` durante pruebas; sólo `true` con aprobación y controles finales |
| storage | backend durable privado por tenant; filesystem temporal no es válido para activación |
| scheduler | proveedor autorizado con `CRON_SECRET`, propietario único y alertas |
| secretos de conexión | SecretProvider real; nunca valores en payloads, logs o repositorio |

## Validación contra el repositorio

- WhatsApp Catalog, Canonical Order, Workbench, delivery, audit, multitenancy y recovery están cubiertos por la suite crítica: `207 tests, 0 failures, 0 errors`.
- Generic REST existe como contrato/mocking y tiene pruebas de mapping, idempotencia y seguridad.
- En el commit actual `generic_rest` informa `runtime_enabled=false`, `mock_only=true` y `credentials_source=future_secret_provider`.
- Sage y Odoo informan `runtime_enabled=false`, `mock_only=true`.
- No existe una implementación/configuración de SecretProvider gestionado, storage durable final ni alertas externas en este worktree.

## Criterio de activación

El perfil permanece `OPEN` hasta que exista un runtime autorizado donde el destino REST aprobado sea realmente ejecutable, los secretos se resuelvan desde el proveedor aprobado, storage sobreviva `write → restart → read`, y web/worker/scheduler, cron, alertas, backup/restore y browser critical path tengan evidencia.

Este documento formaliza el contrato; no habilita tráfico externo ni cambia flags de runtime.
