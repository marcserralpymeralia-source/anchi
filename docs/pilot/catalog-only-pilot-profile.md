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
| `PILOT_PROFILE` | `CATALOG_ONLY_PILOT` para aplicar readiness estricta |
| `TENANT_DB_MODE` | `external` |
| `MASTER_DATABASE_URL` / tenant DB | PostgreSQL gestionado y persistente |
| workers internos del web | desactivados; worker dedicado |
| `IMAP_SYNC_ENABLED` / `EMAIL_AUTO_SYNC_ENABLED` | `false` |
| audio | no anunciado ni incluido en el contrato de entrada |
| módulos habilitados | `PILOT_ENABLED_MODULES=whatsapp_catalog,generic_rest` |
| `PILOT_GENERIC_REST_RUNTIME_ENABLED` / `EXTERNAL_CONNECTOR_HTTP_ENABLED` | `false` por defecto; ambos sólo `true` tras aprobar el destino sandbox, mapping, secreto y egress |
| `PILOT_SCHEDULED_JOB_PROFILE` | `CATALOG_ONLY_PILOT`; sólo procesa jobs del flujo pedido/entrega |
| storage | `ATTACHMENT_STORAGE_BACKEND=vercel_blob`, store privado compartido; filesystem temporal no satisface readiness piloto |
| scheduler | proveedor autorizado con `CRON_SECRET`, propietario único y alertas |
| secretos de conexión | SecretProvider real; nunca valores en payloads, logs o repositorio |

## Validación contra el repositorio

- WhatsApp Catalog, Canonical Order, Workbench, delivery, audit, multitenancy y recovery están cubiertos por la suite crítica: `207 tests, 0 failures, 0 errors`.
- Generic REST conserva mapping/idempotencia existentes y tiene un camino real gated por connection activa, flags runtime/HTTP, perfil piloto, SecretProvider y EgressPolicy; no hay fallback a mock. La salida de red permanece apagada por defecto.
- Sage y Odoo informan `runtime_enabled=false`, `mock_only=true`.
- `EnvironmentSecretProvider` resuelve refs tenant/connection desde secrets del runtime, pero no se provisionó el entorno; Blob/Neon/Render y alertas no se validaron externamente.

## Criterio de activación

El perfil permanece `OPEN` hasta que exista un runtime autorizado donde el destino REST sandbox aprobado sea ejecutable, los secretos se resuelvan desde el entorno aprobado, storage sobreviva `write → restart → read`, y web/worker/scheduler, cron, alertas, backup/restore y browser critical path tengan evidencia.

Este documento formaliza el contrato; no habilita tráfico externo ni cambia flags de runtime.
