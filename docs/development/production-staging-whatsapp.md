# Produccion, Staging y WhatsApp

## Hito A

- `Dockerfile` y `docker-compose.yml` dejan una ruta minima para web, worker y PostgreSQL.
- `Procfile` documenta el arranque simple para plataformas tipo PaaS.
- La readiness ahora expone:
  - estado del master,
  - estado del esquema master,
  - estado del tenant activo,
  - estado del esquema tenant,
  - workers arrancados,
  - almacenamiento local.
- El baseline de tests sigue sobre SQLite, y el smoke PostgreSQL queda preparado en `backend/tests/test_postgresql_smoke.py` para entornos con base real.

## Hito B

- WhatsApp se integra como adaptador del modelo comun de mensajes.
- La configuracion vive por tenant en `channel_settings` del canal `whatsapp`.
- El webhook entra por `POST /webhooks/whatsapp/{company_slug}`.
- La verificacion usa `verify_token`.
- La firma usa `X-Hub-Signature-256`.
- Los eventos entrantes se normalizan en `InboundMessage` y `Conversation`.
- La entrada se encola como `process_inbound_message` para reutilizar el pipeline comun.

## Limitaciones actuales

- No se ha levantado Docker ni PostgreSQL en este entorno de trabajo.
- La validacion PostgreSQL y la restauracion real quedan preparadas, pero no ejecutadas aqui.
- El envio saliente de WhatsApp queda como siguiente capa natural para una iteracion posterior.

