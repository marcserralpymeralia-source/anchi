# Matriz de fallos y recuperación del piloto

Esta matriz define qué se detecta, qué hace ANCHI, qué puede hacer soporte y cuándo se detiene el piloto. Ninguna acción de soporte debe saltarse el `company_id`, la idempotencia o el registro de auditoría.

| Escenario | Detección | Comportamiento automático | Acción inmediata de soporte | Parada / escalado |
|---|---|---|---|---|
| Web no responde | Uptime/`/health/live` | No procesa peticiones nuevas | Comprobar despliegue, logs y dependencia DB; no reintentar webhooks a ciegas | Parar nuevas pruebas si supera el SLA acordado |
| Readiness no OK | `/health/ready`, DB/migración/storage | Debe impedir tráfico operativo o marcar degradado | Capturar JSON y no abrir el piloto; resolver dependencia indicada | P0 si se sirve tráfico con storage/DB no disponible |
| Master DB caída | health master, errores de login/tenant | Lifespan puede arrancar degradado y deshabilitar workers | No crear tenants ni cambiar configuración; restaurar/recuperar DB | Escalar a plataforma; no usar datos reales durante degradación |
| Tenant no migrado | `tenant_migration_report`, 503 y worker blocked | Worker omite tenant incompatible | Ejecutar report/dry-run; no forzar `create_all` manualmente en producción | Parar tenant afectado; migrar con backup |
| Webhook duplicado | mismo `wamid`/external event | Idempotencia evita segundo mensaje/pedido/outbox | Buscar por ID externo y confirmar que sólo existe un resultado | Escalar si hay más de un pedido o entrega |
| Cliente/producto desconocido | estado `pending_review`/doubt reason | No debe exportar | Resolver identidad en workbench; registrar corrección | No forzar exportación sin motivo y actor |
| Pedido ambiguo | review reasons / validación | No crea entrega automática mientras requiere revisión | Revisar línea, cliente, cantidad y unidad; confirmar sólo con evidencia | Escalar si el catálogo maestro no permite resolver |
| Confirmación manual | pedido pasa a confirmado | La ruta manual confirma/aprende, pero no crea por sí sola outbox | Seguir el paso explícito de exportación definido para el piloto y anotar delivery ID | Si no existe delivery tras el procedimiento, detener flujo y escalar G-07 |
| Worker detenido | heartbeat ausente, backlog creciente | No hay procesamiento async; stale recovery sólo ocurre al volver a ejecutar ciclo | Comprobar que no hay dos workers, levantar uno dedicado, revisar stale jobs | Parar entrada si backlog supera el límite acordado |
| Job `retrying` | jobs monitor: attempts/next run | Backoff exponencial y reintento | Esperar ventana; no duplicar job manualmente | Escalar si supera `max_retries` o backlog envejece |
| Entrega timeout antes de crear | `delivery.failed` retryable/timeout | Reintenta con idempotency key | Dejar que el worker reintente; revisar audit | Escalar tras máximo de intentos |
| Éxito remoto y timeout local | delivery attempt `remote_commit_unknown` | Reconciliación busca pedido externo antes de crear otro | No reintentar creando un nuevo outbox; reanudar la misma entrega | Escalar si no se puede reconciliar |
| Error permanente 4xx/auth | status `failed`, `export_failed` | No reintenta automáticamente | Corregir credencial/configuración; reabrir una entrega con mecanismo aprobado | No repetir mientras el destino siga rechazando |
| DLQ/entrega terminal | `OrderDelivery.status=failed` | Queda evidencia en delivery, attempt y audit | Usar listado/retry soportado; si sólo existe función interna, escalar a backend y no manipular DB | P1: no operar manualmente en DB sin autorización |
| Storage no escribible | error de adjunto, readiness incompleto | Mensaje puede quedar error; WhatsApp media puede ser retryable | No borrar registros; comprobar volumen/Blob/permisos y conservar payload | Parar ingestión de adjuntos; P0 si hay pérdida o exposición |
| Adjunto corrupto/oversize | extraction status/error | Se conserva mensaje y estado de extracción | Marcar para revisión; pedir original por canal seguro | Escalar si se pierde el original |
| Email sin credencial | sync blocked | No descarga | Renovar credencial/configurar IMAP manualmente; no guardar password en ticket | Excluir email si no se resuelve |
| Error de un mensaje IMAP | `email.sync.message_error`, contador errors | Continúa con los siguientes mensajes | Guardar UID, error type, mailbox y correlation ID; reintentar sólo el rango | Escalar si hay pérdida/duplicación |
| WhatsApp media/audio | attachment `transcription_pending`, failed o unsupported | No debe entrar al pipeline hasta estar listo | Para primer piloto, rechazar/excluir audio; conservar estado y media ID | No anunciar soporte de audio |
| Cron no ejecuta | ausencia de última ejecución, 401/403 | No hay jobs/email sync/snapshots programados | Validar `CRON_SECRET`, path, schedule y logs del proveedor | Escalar a plataforma; no activar polling interno duplicado |
| Config drift | manifest distinto entre web/worker/cron | Puede producir workers duplicados o secretos faltantes | Comparar checksum/manifest; desplegar de nuevo con configuración aprobada | Parar si web y worker apuntan a DB distintas |
| Sospecha de cruce de tenant | audit/context tenant distinto al recurso | Las queries deben filtrar `company_id` | Cerrar sesión, conservar request/correlation IDs, no copiar datos | P0: congelar acceso y escalar a seguridad |
| Cuenta o tenant a retirar | solicitud de offboarding | No hay borrado automático completo documentado | Desactivar accesos/canales, exportar evidencia y aplicar política aprobada | No declarar borrado completo sin revisar DB, blobs, logs y backups |
| Deploy defectuoso | smoke post-deploy, errores nuevos | Health/readiness debería bloquear | Detener promoción, preservar logs y volver a la versión anterior si es segura | Rollback formal, nunca downgrade improvisado |
| Restore fallido | validación de backup incompleta | No se abre el servicio restaurado | Mantener servicio original, conservar snapshot y escalar DBA | P0 para go-live si no existe restore probado |

## Procedimiento de parada

1. Identificar tenant, pedido, inbound, delivery/job y `request_id`/`correlation_id`.
2. Pausar la entrada afectada o el canal completo; no borrar mensajes ni jobs.
3. Si hay riesgo de duplicado remoto, detener reintentos y conservar `idempotency_key`.
4. Capturar health, audit, job attempts y estado de DB/storage en el ticket.
5. Declarar el piloto en estado **STOPPED** si hay P0, pérdida de datos, cruce de tenant, storage no durable o falta de backup restaurable.

## Procedimiento de rollback

1. Parar web, worker y cron del entorno afectado.
2. Impedir nuevos webhooks/entradas en el proveedor o colocar el canal en pausa.
3. Identificar el último backup/snapshot válido y registrar RPO/RTO.
4. Restaurar primero una copia temporal; validar master, tenant, mensajes, pedidos, jobs, deliveries y audit.
5. Restaurar la base productiva sólo con aprobación del responsable de plataforma/DBA.
6. Restaurar/reasociar el storage de adjuntos y comprobar una lectura real.
7. Ejecutar migrations report, readiness, login, un pedido sintético y un delivery idempotente.
8. Reactivar cron, worker y web en ese orden; monitorizar antes de reabrir entradas.

No existe downgrade automático en esta fase. El rollback es restauración de snapshot/backup o redeploy de la versión anterior con esquema compatible.
