# Jobs fiables

## Arquitectura actual

La app mantiene un sistema de jobs por tenant sobre la DB operativa de cada compania. No se ha introducido una cola externa nueva.

Flujo:

1. Se encola una operacion idempotente.
2. El worker toma un unico job elegible.
3. Se registra el intento.
4. Se ejecuta el efecto real.
5. Se marca exito, reintento o fallo permanente.
6. Si el worker cae, el job puede recuperarse tras `JOB_STALE_AFTER_SECONDS`.

## Tipos de jobs

| Tipo | Uso |
|---|---|
| `email_sync` | Lectura IMAP con auto procesado opcional |
| `process_recent_emails` | Procesado rapido de correos recientes |
| `backfill_imap` | Recorrido historico controlado por fecha |
| `process_pending_emails` | Procesado de correos pendientes |
| `process_email` | Procesado de un correo individual |
| `process_order` | Procesado de un pedido a traves de su correo |
| `import_confirm` | Confirmacion final de importaciones |
| `import_file` | Preparacion de importacion desde archivos |
| `export_order` | Generacion de exportacion de pedido |
| `export_order_ftp` | Exportacion y envio por FTP/SFTP |
| `bulk_order_action` | Acciones masivas de bandeja/pedidos |

## Estados y transiciones

Estados actuales:

- `queued`
- `running`
- `retrying`
- `success`
- `failed`
- `cancelled`

Transiciones principales:

- `queued -> running`
- `running -> success`
- `running -> retrying`
- `running -> failed`
- `retrying -> running`
- `queued/retrying -> cancelled`

No se reactiva un job exitoso por defecto. Para una nueva ejecucion se necesita una clave de idempotencia distinta o una accion manual explicita.

## Idempotencia

La clave de idempotencia se calcula con:

`{job_type}:{payload_canónico}`

Reglas:

- un job equivalente activo se reutiliza;
- un job equivalente ya exitoso se reutiliza;
- el payload debe ser JSON serializable;
- el payload no puede contener secretos como `password`, `api_key`, `client_secret`, `refresh_token`, `access_token`, `imap_password`, `smtp_password`, `private_key` o `database_url`.

## Adquisicion exclusiva

El worker selecciona jobs elegibles y los marca como `running` con bloqueo temporal y propietario de worker.

Cada adquisicion:

- incrementa `attempt_count`;
- crea un registro `JobAttempt`;
- fija `lock_owner`;
- fija `lock_until`;
- respeta `next_retry_at`.

## Intentos y reintentos

- `attempt_count` sube al adquirir el job.
- `retry_count` sube cuando se programa un nuevo reintento.
- `JOB_MAX_ATTEMPTS` limita el total de ejecuciones.
- `JOB_RETRY_BASE_SECONDS` y `JOB_RETRY_MAX_SECONDS` controlan el backoff.
- El backoff es exponencial y determinista.

## Jobs abandonados

Si un job sigue en `running` mas alla de `JOB_STALE_AFTER_SECONDS`, el worker lo recupera de forma controlada.

La recuperacion:

- conserva el historial del intento;
- marca el intento como `abandoned`;
- programa reintento si aun quedan intentos;
- pasa a `failed` si ya no quedan intentos.

## Tenant

Todo job operativo pertenece a una compania concreta. El worker resuelve la DB del tenant a traves del master y no usa rutas arbitrarias desde el payload.

## Monitor y retry manual

El monitor muestra:

- tipo;
- tenant;
- estado;
- intentos;
- maximo;
- fechas;
- lock;
- error seguro;
- historial de intentos en el detalle.

El retry manual conserva el historial y solo reprograma el mismo job cuando procede.

## Regla para crear nuevos jobs

Crear un nuevo job solo cuando:

- cambia la clave de idempotencia;
- cambia la version/intencion de la operacion;
- o hay una reactivacion manual explicita.

## Checklist de seguridad

- No hay secretos en payloads.
- No se usa `database_url` del usuario.
- No se cruza tenant.
- No se ejecutan dos workers sobre el mismo job.
- No hay retries infinitos.
- El historial se conserva.
