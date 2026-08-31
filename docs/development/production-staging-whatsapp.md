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
- El alta principal usa el modo de coexistencia (`whatsapp_business_app_onboarding`) para que el cliente conserve el mismo numero activo en WhatsApp Business App y, a la vez, autorice a Anchi a recibir sus mensajes mediante Cloud API.
- El backend valida que el numero pertenece al WABA autorizado y que Meta lo identifica como numero de WhatsApp Business App. En coexistencia no ejecuta `/{PHONE_NUMBER_ID}/register`; esa llamada queda solo como compatibilidad con altas nuevas de Cloud API.
- El WABA se suscribe mediante `/{WABA_ID}/subscribed_apps` y se le asigna el callback del tenant.
- El webhook tenant entra por `POST /webhooks/whatsapp/{company_slug}` y se asigna con `override_callback_uri` durante la suscripcion.
- `GET|POST /webhooks/whatsapp` queda como callback base y fallback para la configuracion de la aplicacion de Meta.
- La verificacion usa un token global para el callback base y un token aleatorio cifrado por tenant para el callback asignado.
- La firma usa `X-Hub-Signature-256` con el App Secret global, que permanece exclusivamente en servidor.
- Los mensajes entrantes en vivo se normalizan en `InboundMessage` y `Conversation` y se encolan como `process_inbound_message` para reutilizar el pipeline comun. Las direcciones telefonicas que Meta envia como `wa_id`/`from` numerico se canonizan con prefijo `+`, para que coincidan con los contactos internacionales guardados y con los destinatarios de las respuestas.
- Los mensajes enviados desde WhatsApp Business App (`smb_message_echoes`) se guardan como salientes y nunca se procesan como pedidos nuevos.
- El historial compartido durante el alta (`history`) se importa conservando direccion y estado, sin reejecutar pedidos antiguos. Los eventos de contactos (`smb_app_state_sync`) se reconocen y auditan sin crear clientes automaticamente.

## Hito C

- Los adjuntos permitidos de WhatsApp se descargan desde dominios de Meta, se validan por tipo y tamano, y se persisten antes de extraer su contenido.
- Los documentos compatibles (`PDF`, `DOCX` y `TXT`) entran en el pipeline comun mediante `MessageAttachment`; los audios quedan almacenados y pendientes de transcripcion si no hay proveedor configurado. El formato binario `.DOC` no se anuncia como compatible porque el extractor comun no puede leerlo.
- La descarga vuelve a validar el tipo MIME que devuelve Meta antes de leer el binario; una discrepancia o un tipo no permitido queda como error no procesable y no se persiste.
- El pipeline comun identifica WhatsApp por canal y mantiene la misma ruta de clasificacion, resolucion, score y `Order` que el resto de entradas.
- Las respuestas manuales y automaticas usan reservas persistentes con clave de idempotencia antes de llamar a Meta. Un resultado incierto queda en `send_unknown` y no se reintenta automaticamente.
- Las respuestas de texto respetan la ventana de conversacion; fuera de ella se exige una plantilla aprobada. Los estados `sent`, `delivered`, `read` y `failed` se reconcilian mediante los webhooks de estado.

## Checklist UAT MULET

Validado localmente con pruebas automatizadas:

1. Verificacion de tenant, aislamiento por WABA/numero y firma HMAC.
2. Ingesta de texto, deduplicacion, encolado y reintento del job.
3. Descarga/persistencia de documentos y tratamiento de adjuntos no compatibles.
4. Pipeline comun hasta resolucion de cliente/producto, score y `Order`/revision.
5. Envio de texto, plantilla y media con idempotencia, estados de entrega y fallos seguros.

La validacion reproducible se ejecuta desde `backend` con:

```powershell
python -m unittest tests.test_whatsapp_integration tests.test_whatsapp_conversation_orders tests.test_whatsapp_conversation_semantics tests.test_whatsapp_auto_responses tests.test_whatsapp_demo_simulator
```

La simulacion local (`python -m scripts.simulate_whatsapp_demo --company-slug anchi-demo --enqueue`) demuestra la ingesta y el encolado sin llamar a Meta. Para ejecutar tambien el pipeline automatico hace falta configurar un proveedor de IA de desarrollo; sin el, el worker deja el job en estado reintentable y no se presenta como UAT completo.

El smoke UAT `WhatsAppIntegrationTests.test_mulet_local_uat_covers_bidirectional_flow_without_duplicates` recorre con proveedores HTTP simulados el texto, la deduplicacion, el `Order`, la extraccion de PDF, la respuesta saliente, el delivery y un fallo incierto sin repetir el envio.

Pendiente de ejecutar con la cuenta MULET real:

1. Recibir un mensaje real y confirmar su aparicion en la bandeja.
2. Recibir un documento y un audio reales y comprobar persistencia y lectura/transcripcion.
3. Reenviar el mismo webhook y confirmar que no se duplica el mensaje ni el job.
4. Procesar un pedido real hasta `Order`/revision y validar cliente y productos.
5. Responder desde Anchi y confirmar en Meta el mensaje y sus estados de entrega.
6. Repetir un fallo controlado de proveedor y comprobar que no duplica el envio.

## Configuracion de Meta

1. Crear una aplicacion de tipo Business en Meta, anadir WhatsApp y habilitar Facebook Login for Business.
2. Crear una configuracion de Embedded Signup v4 compatible con WhatsApp Business App onboarding y copiar su Configuration ID.
3. Configurar como callback base `https://TU_DOMINIO/webhooks/whatsapp` y usar el mismo valor de `META_WHATSAPP_VERIFY_TOKEN` guardado en el servidor.
4. Suscribir en el producto Webhooks de la aplicacion los campos `messages`, `history`, `smb_app_state_sync`, `smb_message_echoes` y `account_update`.
5. En produccion, solicitar Advanced Access para `whatsapp_business_management` y `whatsapp_business_messaging`, completar la verificacion empresarial y el alta como Tech Provider que correspondan. La [aplicacion de referencia oficial de Meta](https://github.com/fbsamples/business-messaging-sample-tech-provider-app) documenta este recorrido y el `featureType` de coexistencia.
6. Publicar la instancia bajo HTTPS y configurar:

```dotenv
APP_URL="https://anchi.example.com"
META_APP_ID="..."
META_APP_SECRET="..."
META_EMBEDDED_SIGNUP_CONFIG_ID="..."
META_GRAPH_API_VERSION="v24.0"
META_EMBEDDED_SIGNUP_VERSION="v4"
META_WHATSAPP_VERIFY_TOKEN="..."
META_OAUTH_REDIRECT_URI=""
```

`META_WHATSAPP_REGISTRATION_PIN` no es necesaria para coexistencia. Solo debe configurarse, con exactamente seis digitos, si se mantiene tambien el flujo alternativo para registrar numeros nuevos de Cloud API. Los valores sensibles deben vivir en el gestor de secretos del despliegue y nunca en Git. Para probar el flujo real desde local hace falta un tunel HTTPS y registrar esa URL en Meta; el servidor HTTP local solo permite revisar la interfaz y los estados seguros de configuracion.

## Limitaciones actuales

- No se ha levantado Docker ni PostgreSQL en este entorno de trabajo.
- La validacion PostgreSQL y la restauracion real quedan preparadas, pero no ejecutadas aqui.
- El envio saliente y su idempotencia ya estan implementados; falta comprobarlos contra la cuenta y los callbacks reales de MULET.
- La elegibilidad final del numero para coexistencia la decide Meta durante Embedded Signup. El cliente debe usar una version compatible de WhatsApp Business App y aceptar, si lo desea, compartir contactos e historial.

