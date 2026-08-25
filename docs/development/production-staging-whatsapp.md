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
- El alta se realiza con [WhatsApp Embedded Signup de Meta](https://www.postman.com/meta/whatsapp-business-platform/folder/b1a1oq8/step-1-embed-the-signup-flow), sin pedir al administrador que copie tokens o secretos en formularios.
- El navegador recibe solo `META_APP_ID`, el Configuration ID y las versiones publicas. El codigo temporal se intercambia en backend usando `META_APP_SECRET`.
- El backend valida que el numero pertenece al WABA autorizado, registra el telefono y suscribe el WABA mediante `/{WABA_ID}/subscribed_apps`.
- El webhook tenant entra por `POST /webhooks/whatsapp/{company_slug}` y se asigna con `override_callback_uri` durante la suscripcion.
- `GET|POST /webhooks/whatsapp` queda como callback base y fallback para la configuracion de la aplicacion de Meta.
- La verificacion usa un token global para el callback base y un token aleatorio cifrado por tenant para el callback asignado.
- La firma usa `X-Hub-Signature-256` con el App Secret global, que permanece exclusivamente en servidor.
- Los eventos entrantes se normalizan en `InboundMessage` y `Conversation`.
- La entrada se encola como `process_inbound_message` para reutilizar el pipeline comun.

## Configuracion de Meta

1. Crear una aplicacion de tipo Business en Meta, anadir WhatsApp y habilitar Facebook Login for Business.
2. Crear una configuracion de Embedded Signup y copiar su Configuration ID.
3. Configurar como callback base `https://TU_DOMINIO/webhooks/whatsapp` y usar el mismo valor de `META_WHATSAPP_VERIFY_TOKEN` guardado en el servidor.
4. En produccion, solicitar Advanced Access para `whatsapp_business_management` y `whatsapp_business_messaging` y completar la verificacion empresarial que corresponda. La [aplicacion de referencia oficial de Meta](https://github.com/fbsamples/business-messaging-sample-tech-provider-app) documenta este recorrido.
5. Publicar la instancia bajo HTTPS y configurar:

```dotenv
APP_URL="https://anchi.example.com"
META_APP_ID="..."
META_APP_SECRET="..."
META_EMBEDDED_SIGNUP_CONFIG_ID="..."
META_GRAPH_API_VERSION="v24.0"
META_EMBEDDED_SIGNUP_VERSION="v4"
META_WHATSAPP_REGISTRATION_PIN="000000"
META_WHATSAPP_VERIFY_TOKEN="..."
META_OAUTH_REDIRECT_URI=""
```

`META_WHATSAPP_REGISTRATION_PIN` debe contener seis digitos. Los valores sensibles deben vivir en el gestor de secretos del despliegue y nunca en Git. Para probar el flujo real desde local hace falta un tunel HTTPS y registrar esa URL en Meta; el servidor HTTP local solo permite revisar la interfaz y los estados seguros de configuracion.

## Limitaciones actuales

- No se ha levantado Docker ni PostgreSQL en este entorno de trabajo.
- La validacion PostgreSQL y la restauracion real quedan preparadas, pero no ejecutadas aqui.
- El envio saliente de WhatsApp queda como siguiente capa natural para una iteracion posterior.

