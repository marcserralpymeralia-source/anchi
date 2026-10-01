# Runbook de soporte del piloto

## Objetivo

Resolver incidencias sin perder trazabilidad, sin duplicar pedidos y sin cruzar tenants. El identificador principal del ticket debe ser el `company_id` más uno de `order_id`, `delivery_id`, `job_id`, `inbound_message_id` o `correlation_id`.

## Datos mínimos del ticket

```text
Entorno / commit:
Tenant y company_id:
Hora UTC:
Usuario/rol:
Canal y external ID:
order_id / delivery_id / job_id / attempt_id:
request_id / correlation_id:
Síntoma y captura:
Último estado conocido:
Acción ejecutada:
Resultado y siguiente paso:
```

Nunca copiar al ticket passwords, access tokens, cookies, secretos, payloads completos con PII o adjuntos de cliente. Usar IDs y extractos redactados.

## Severidades de soporte

- **S0**: posible cruce de tenants, pérdida de datos, exposición de secreto o duplicado externo no controlado. Pausar canal y escalar a seguridad/plataforma.
- **S1**: el flujo principal no puede recibir, revisar o entregar pedidos; worker, DB, storage, cron o destino caído. Pausar la entrada si crece el backlog.
- **S2**: un pedido/cliente requiere intervención manual, fallo de un proveedor secundario o extracción incompleta.
- **S3**: UX, documentación o mejora no urgente.

## Diagnóstico seguro

1. Consultar `/health/live` y `/health/ready`.
2. Confirmar tenant, sesión y permisos; no abrir el recurso desde otro tenant para comparar.
3. Localizar por IDs en jobs, logs y `/integrations/audit`.
4. Revisar estado y attempts antes de reintentar.
5. Verificar si hubo commit remoto antes de repetir una entrega.
6. Registrar toda intervención y mantener el payload original.

## Procedimientos frecuentes

### Pedido no aparece

Comprobar webhook recibido, validación de firma/identidad, `external_event_id`, estado del inbound y deduplicación. Si cliente o producto es desconocido, debe quedar en revisión; no crear manualmente un pedido duplicado.

### Pedido en revisión

Abrir workbench, confirmar cliente, producto, cantidad, unidad y motivo. Guardar la corrección. Confirmar sólo cuando desaparezcan los bloqueos. En el piloto, seguir el paso explícito de **Exportar/encolar entrega** y capturar el `delivery_id`; la confirmación manual no se debe interpretar automáticamente como entrega.

### Entrega en retrying

Esperar la siguiente ventana de backoff salvo que el destino esté claramente recuperado. Revisar `last_error_type`, `next_attempt_at`, attempt count y audit. No crear otro outbox.

### Entrega failed

Clasificar: credencial/permisos/configuración (permanente), rate limit/timeout/health (reintentable), o commit remoto desconocido (reconciliación). Corregir primero el destino. Usar la operación de retry aprobada; si no está disponible en UI, escalar a backend y no manipular la base directamente.

### Worker sin progreso

Comprobar proceso, heartbeat, backlog, locks y `JOB_STALE_AFTER_SECONDS`. Verificar que web no ejecuta otro worker interno. Reiniciar el worker una vez, observar recuperación de stale jobs y confirmar que no se han duplicado claims.

### Cron sin ejecuciones

Probar el endpoint con secreto desde el proveedor autorizado. Revisar schedule, zona horaria, 401/403, timeout y última ejecución. No habilitar además un polling interno sin decisión explícita.

### Storage o adjunto fallido

No borrar el registro. Comprobar readiness, volumen/Blob, cuota, permisos y lectura de una referencia existente. Detener entrada de adjuntos si existe riesgo de pérdida. Reintentar sólo después de confirmar persistencia.

### Email/audio fuera de contrato

Informar que el piloto no cubre el canal/capacidad. Conservar el evento en estado pendiente o error recuperable y abrir ticket S2. No activar autodiscovery, backfill o audio en producción para “probar”.

## Intervenciones manuales permitidas

- Pausar/reactivar canal o integración mediante la UI y rol autorizado.
- Corregir mappings y datos maestros con actor auditado.
- Reintentar un job/entrega cuando exista operación soportada y se haya eliminado la causa.
- Reiniciar worker/web mediante plataforma, conservando logs.
- Ejecutar report/dry-run de migraciones.

## Intervenciones prohibidas sin aprobación

- Editar `OrderDelivery`, `BackgroundJob`, pedidos o audit directamente en DB.
- Borrar inbound, mensajes, adjuntos, jobs o backups para “limpiar” el incidente.
- Reutilizar una idempotency key con otro pedido.
- Cambiar secretos en tickets o chat.
- Activar un conector, canal o worker adicional durante una incidencia.

## Cierre de incidente

El ticket sólo se cierra con: causa, tenant afectado, IDs, acción, prueba de recuperación, comprobación de no duplicado, evidencia de audit y decisión sobre si actualiza el registro de gaps.
