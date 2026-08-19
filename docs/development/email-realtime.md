# Recepcion de email en tiempo casi real

## Objetivo

Anchi debe recibir correo por tenant sin mezclar credenciales, checkpoints ni mensajes entre companias. La recepcion en tiempo casi real no se ejecuta dentro de Vercel serverless: requiere un proceso persistente separado.

## Aislamiento por tenant

- La configuracion IMAP vive en la base tenant, en `email_settings`.
- El estado operativo de lectura vive en master, en `email_sync_state`, separado por `company_id` y `channel_key=email`.
- Cada reconciliacion abre la base tenant desde `tenant_databases.database_url` y guarda correos solo con el `company_id` de ese tenant.
- Los checkpoints `uidvalidity` y `last_seen_uid` son independientes por compania.

## Worker persistente

El proceso recomendado para recepcion continua es:

```bash
cd /ruta/a/backend
APP_ENV=production python -m app.workers.email_listener
```

El listener:

- recorre tenants activos con base operativa registrada;
- respeta `auto_sync_enabled`;
- actualiza `listener_status`, `listener_owner` y `listener_last_heartbeat_at`;
- ejecuta reconciliacion incremental con UID;
- aplica backoff mediante `next_run_at` y `frequency_seconds`;
- no guarda credenciales en logs.

En Vercel no se inicia este listener. Vercel puede servir la UI y rutas manuales, pero no debe considerarse un runtime persistente de correo.

## Sincronizacion manual

Los botones manuales ya no dependen de un worker vivo para mostrar un resultado. Crean un job auditado y lo ejecutan inline, devolviendo el resumen real:

- encontrados;
- descargados;
- importados;
- duplicados;
- descartados;
- errores;
- checkpoint anterior y final.

Esto evita el caso de “Sincronizacion iniciada” sin correos importados.

## Historico inicial y backfill

El valor por defecto debe ser `Solo correos nuevos desde ahora`.

Modos soportados:

- `new`: guarda el UID maximo actual como punto de partida y no importa historico.
- `7d`, `30d`, `100`, `custom`: importan historico acotado por fecha/limite.

No existe modo ilimitado desde la interfaz. El limite inicial se acota a 100 y cada reconciliacion a un maximo seguro.

## Pipeline

Cuando se importa un email:

1. Se guarda `Email`.
2. Se crea `InboundMessage`.
3. Si `auto_process_on_fetch` esta activo, se encola `process_email`.
4. El pipeline existente decide si crea o actualiza pedido.

El backfill historico no auto-procesa por defecto.

## Recuperacion

La prevencion de duplicados usa:

- `external_id` normalizado con mailbox, UIDVALIDITY y UID;
- `message_id` cuando existe;
- UID + UIDVALIDITY por tenant.

Si un mensaje falla, se registra el error y se continua con el resto. El checkpoint solo avanza con mensajes procesados o duplicados confirmados.

## No implementado

No se implementa en este bloque:

- OAuth;
- Gmail API;
- Microsoft Graph;
- SMTP;
- WhatsApp;
- redisenos de pipeline.
