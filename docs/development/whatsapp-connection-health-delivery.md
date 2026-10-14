# Entrega de fase

## 1. Objetivo

Completar la pila operativa de WhatsApp para que una reconexion se pueda diagnosticar sin enviar mensajes, la bandeja se actualice con baja latencia, los envios fallidos se puedan recuperar de forma segura y los adjuntos tengan una ruta durable de reintento y alerta.

## 2. Alcance ejecutado

- Comprobacion manual desde Configuracion de WABA, Phone Number ID, numero visible, estado del numero, nombre visible y webhook.
- Deteccion de desajustes entre los activos guardados en Anchi y los activos que devuelve Meta.
- Diagnostico sin mostrar tokens ni secretos.
- Conservacion del codigo de error Meta para clasificar fallos definitivos.
- Actualizacion de la bandeja por SSE corto despues del webhook, con polling como fallback.
- Reintento manual seguro solo para rechazos definitivos; los resultados inciertos quedan bloqueados.
- Alertas deduplicadas para envios no confirmados, fallos de Meta y descargas de media.
- Descarga de media en segundo plano con job durable, reanudacion por adjunto y reintentos del worker.

## 3. Alcance no ejecutado

- No se han enviado mensajes reales ni se han tocado datos de produccion.
- No se ha cambiado Embedded Signup ni la configuracion guardada.
- No se ha implementado WebSocket; SSE corto es la opcion compatible con el despliegue serverless actual.
- No se reintentan automaticamente mensajes cuyo resultado sea incierto porque podrian duplicarse.

## 4. Diagnostico previo

El incidente anterior correspondia al error Meta `131037`: el numero necesitaba aprobacion del nombre visible. Ademas, Anchi podia conservar identificadores de una conexion anterior y no tenia una comprobacion operativa para compararlos con Meta.

## 5. Cambios realizados

- Nuevo endpoint administrativo `POST /settings/channels/whatsapp/health`.
- Consulta de solo lectura a Meta para verificar WABA, telefono, estado `CONNECTED`/`ACTIVE`, nombre visible y numero configurado.
- Indicador visual `Comprobar conexion` en el bloque de WhatsApp conectado.
- Endpoint `GET /whatsapp/inbox/events` con SSE corto y reconexion automatica.
- Fallback de polling para navegadores o proxies que no mantengan SSE.
- Reintento manual de mensajes con estado `failed` o `error` usando una nueva clave de idempotencia.
- Bloqueo explicito de `send_unknown` para evitar duplicados.
- Alertas deduplicadas para errores de salida y de descarga/procesamiento de media.
- Mensaje especifico para la aprobacion pendiente del nombre visible.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/whatsapp/service.py` | Salud, alertas, media y reintentos | Logica de integracion |
| `backend/app/settings/channels_routes.py` | Endpoint de salud | API |
| `backend/app/whatsapp/inbox_routes.py` | SSE y reintento seguro | API |
| `backend/app/static/js/whatsapp-embedded-signup.js` | Mostrar salud de Meta | Interfaz |
| `backend/app/templates/components/whatsapp_embedded_signup.html` | Diagnostico de conexion | Interfaz |
| `backend/app/templates/whatsapp/inbox.html` | SSE y fallback | Interfaz |
| `backend/app/templates/whatsapp/_chat_live.html` | Accion de reintento | Interfaz |
| `backend/tests/test_whatsapp_integration.py` | Salud, alertas y reintentos | Tests |
| `backend/tests/test_whatsapp_inbox.py` | Clasificacion de `131037` | Tests |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/development/whatsapp-connection-health-delivery.md` | Registro de la pila implementada |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| SSE corto | Compatible con Vercel/serverless y permite despertar la bandeja tras el webhook | WebSocket persistente |
| Polling como fallback | Mantiene compatibilidad si un proxy corta SSE | Falla total de la bandeja |
| Reintentar solo `failed` | El rechazo del proveedor es definitivo | Repetir `send_unknown` a ciegas |
| Idempotencia nueva en el reintento | Permite una nueva tentativa sin repetir una clave ya consumida | Reutilizar la clave y ocultar el historial |
| Alertas deduplicadas | Evita llenar el centro de alertas durante reintentos | Crear una alerta por cada intento |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `$env:APP_ENV='test'; $env:DEBUG='false'; py -m compileall -q app tests` | Correcto |
| `node --check app/static/js/whatsapp-embedded-signup.js` | Correcto |
| `node --check app/static/js/security.js` | Correcto |
| `$env:APP_ENV='test'; $env:DEBUG='false'; py -m unittest tests.test_whatsapp_integration tests.test_whatsapp_inbox` | 64 tests OK en 209.108 s |
| Tests focalizados de salud, nombre pendiente, rechazo Meta y clasificacion `131037` | 4 tests OK en la validacion previa |
| Test de reintento definitivo y resolucion de alerta | 1 test OK |
| `git diff --check` | Correcto; solo avisos LF/CRLF de Git |

## 10. Tests anadidos o modificados

- Salud de conexion con WABA y Phone Number ID coherentes.
- Nombre visible en `PENDING_REVIEW` como atencion requerida.
- Reintento de un rechazo definitivo con nueva idempotencia.
- Resolucion de la alerta cuando el reintento funciona.
- Alerta para un resultado `send_unknown`.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| La bandeja reacciona al webhook sin esperar siempre al polling | Cumplido | SSE corto y evento de revision |
| Un proxy incompatible no rompe la bandeja | Cumplido | Fallback de polling |
| Un mensaje rechazado se puede reintentar sin reutilizar la clave | Cumplido | Test de reintento |
| Un resultado incierto no se duplica automaticamente | Cumplido | Estado `send_unknown` y alerta |
| Media con error queda en cola y visible | Cumplido | Job durable, placeholder y alerta |

## 12. Riesgos y observaciones pendientes

- Si el proxy no mantiene SSE, se usara polling automaticamente.
- La reconciliacion de `send_unknown` depende del webhook de estados de Meta; no se fuerza un reenvio ciego.
- La suite ejecutada emite avisos de deprecacion del adaptador datetime de SQLite y de conexiones SQLite abiertas en tests existentes; no producen fallos.
- Se detecto fuera de este alcance una incompatibilidad existente en `tests.test_setup_onboarding`: `TemplateResponse` usa una firma antigua de Starlette cuando no esta activo el perfil de rendimiento.

## 13. Desviaciones respecto al alcance inicial

Se uso SSE corto en vez de WebSocket porque el despliegue es serverless y conserva un fallback funcional.

## 14. Estado final de Git

Pendiente de crear el commit y hacer push tras la revision final del diff. No se han incluido secretos ni datos de produccion.

## 15. Recomendacion para la siguiente fase

Medir en produccion el tiempo webhook -> bandeja, añadir un indicador de ultimo webhook recibido y revisar la alerta de calidad del numero cuando Meta la devuelva.
