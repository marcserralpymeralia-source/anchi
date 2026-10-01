# Block 11 — Fresh deployment rehearsal

## Alcance

Rehearsal local, sintético y no productivo para validar que una instalación nueva puede crear sus bases, arrancar la aplicación y completar un flujo browser básico.

## Preparación

- Master SQLite nuevo y tenant SQLite nuevo.
- Compañía y usuario administrador sintéticos.
- `TEMP_STORAGE_DIR` aislado en `/private/tmp`.
- `RUN_INTERNAL_EMAIL_WORKER=false` y `RUN_INTERNAL_JOB_WORKER=false`.
- Credenciales de prueba sin relación con producción.

## Resultado

Uvicorn arrancó en `127.0.0.1:8123`. El browser nativo completó login, acceso al tenant, listado de pedidos, página de entregas fallidas y creación/carga de un pedido de prueba con workbench y adjunto mock.

La migración/upgrade se validó en copias SQLite mediante 24 tests, incluyendo segunda ejecución no-op y preservación de datos.

## Limitaciones

Este rehearsal no prueba PostgreSQL gestionado, Blob/S3, TLS/DNS, secrets manager, backup/restore, cron del proveedor ni persistencia después de recrear el runtime. Por tanto, es evidencia de instalación local, no aprobación de despliegue.
