# Entrega de fase

## 1. Objetivo

Implementar una capa conversacional de WhatsApp que reutilice el runtime de IA y el pipeline de pedidos compartido con Correo, resuelva consultas únicamente con datos aprobados, reúna y aclare los datos del pedido y envíe la conversación a revisión humana tras 12 horas sin mensajes nuevos del cliente.

## 2. Alcance ejecutado

- Sesiones persistentes de pedido por empresa y conversación, con revisiones, corte entre pedidos, vencimiento reiniciable por mensajes del cliente y finalización idempotente mediante jobs.
- Respuestas fundamentadas en FAQ configuradas y catálogo del tenant; identidad asociada solo si el teléfono coincide; cantidades, unidades, referencias y líneas vinculadas a evidencia de mensajes.
- Borrador y preguntas de aclaración deterministas. Una respuesta breve como «sí» solo confirma el mismo resumen de pedido enviado por el agente para la revisión actual.
- Cada turno usa los métodos de clasificación y extracción del pipeline compartido con Correo; no existe una clasificación conversacional paralela en la ruta activa de WhatsApp.
- Los chats históricos permanecen desactivados; cada chat nuevo originado por un mensaje en vivo empieza con el bot activo. El único control visible es un interruptor por chat, sin selector de modos ni paneles adicionales. El debounce y los límites son internos.
- Interruptor persistente y accesible por conversación para alternar entre la IA y la atención humana, también desde estados de derivación o pausa.
- Revalidación justo antes de enviar para cancelar una respuesta si el chat se ha asignado a una persona, el bot se ha desactivado o ya llegó otro mensaje del cliente.
- Al vencer el plazo: consultas sin pedido se cierran sin crear pedido; pedidos incompletos o de identidad no verificada quedan para atención humana; pedidos completos usan un snapshot validado y el pipeline común, siempre con revisión humana y sin confirmar ni exportar automáticamente.
- Medios descargándose quedan pendientes y se reintentan de forma acotada. Audio sin transcripción e imagen sin OCR se derivan a una persona; el bot no finge haber leído su contenido.
- Migración de esquema, estado del bot en el inbox, registro operativo sin contenido del mensaje y pruebas con datos sintéticos. Los webhooks de WhatsApp rechazan cuerpos de más de 2 MiB antes de parsearlos.

## 3. Alcance no ejecutado

- No se envió ni recibió ningún mensaje contra Meta/WhatsApp real; el contenido de producción no se usó para pruebas.
- No se hicieron llamadas al proveedor de IA real: el entorno local no tiene una clave de proveedor; la clasificación/extracción se simularon en pruebas.
- No se ejecutó una migración sobre Neon/PostgreSQL. La revisión local actual no se ha publicado; el estado histórico de la implementación anterior se distingue en la sección 14.
- No hay OCR ni transcripción de audio integrados en este cambio. Si el medio no aporta texto interpretable tras la descarga, se deriva a revisión humana.
- No se añadió un tope monetario diario específico de WhatsApp ni horarios de atención humana. La protección implementada limita los turnos por sesión; el límite monetario global existente no se amplió en esta fase.
- No se probó concurrencia real de PostgreSQL. Las pruebas usan SQLite y verifican las ramas de tenant, idempotencia y control humano, pero SQLite no reproduce los bloqueos `FOR UPDATE` de PostgreSQL.
- Se abrió en modo solo lectura una pestaña autenticada de producción para comprobar la cabecera del chat. La conversación se cargó al abrir la página, pero no se interactuó con ella ni se usó su contenido; la cabecera actual no muestra el interruptor. La prueba local sí verifica el interruptor en el HTML renderizado.
- No se enviaron mensajes ni se modificó información de producción.

## 4. Diagnóstico previo

WhatsApp ya persistía mensajes y podía derivarlos al procesamiento de pedidos, pero no tenía una sesión de negocio fiable ni un cierre de 12 horas reiniciable. El flujo anterior podía lanzar la evaluación final ante frases de cierre o un «sí» antes de resolver dudas. El runtime compartido de Correo sí ofrecía clasificación, evaluación y revisión, por lo que se reutiliza al final mediante un snapshot conversacional validado.

La evaluación turno a turno descrita en el diagnóstico corresponde al estado anterior. En la implementación actual, el clasificador y extractor son los mismos métodos de `UnifiedOrderPipelineService` que usa Correo; solo la orquestación de diálogo sigue siendo específica de WhatsApp.

## 5. Cambios realizados

En esta revisión se eliminó el evaluador conversacional propio de los turnos activos de WhatsApp. El adaptador llama a `UnifiedOrderPipelineService.classify_inbound_text` y `extract_inbound_order`, que delegan en los mismos `_classify` y `_extract` usados por Correo. La clasificación recibe el mensaje actual junto con el mensaje previo relacionado (por ejemplo, una aclaración del bot); el extractor conserva el contexto necesario sin atribuir la pregunta del bot como evidencia del pedido. WhatsApp conserva su gestión de sesión, el borrador conversacional, las respuestas fundamentadas y el cierre tras 12 horas.

Se incorporó el orquestador `backend/app/whatsapp/agent.py`, el modelo/migración de sesión y su integración con jobs y pipeline común. Las consultas de producto/FAQ se limitan a evidencia del tenant; los mensajes recientes se conservan en un contexto acotado; medios no interpretables se derivan; y la finalización verifica revisión, evidencia, identidad y estado humano antes de crear un pedido en revisión. Se añadieron controles/configuración de canal, estados en el inbox y regresiones para el flujo compartido y las carreras de envío.

Al investigar por qué el bot no respondía en producción se encontró un defecto previo en la ruta común de envío: los llamadores pasaban `db` como argumento posicional, pero `await_send_automatic_response` solo aceptaba `**kwargs`. Eso producía `TypeError` antes de contactar con Meta y el `except` lo convertía en un turno aparentemente completado. Se corrigió la firma y ahora los errores de entrega no se tragan; los HTTP 429, que son rechazos definitivos, pueden reintentarse con la misma clave y el mismo texto, mientras que timeouts/resultados ambiguos no se repiten para evitar duplicados.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/alerts/routes.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/core/config.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/jobs/service.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/messages/service.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/settings/channels_routes.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/templates/settings/channels.html` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/whatsapp/agent.py` | Corregir contexto de respuestas breves, límites cronológicos, firma del puente de envío y propagación/reintento seguro de fallos de entrega. | Lógica/regresión |
| `backend/app/whatsapp/conversation_semantics.py` | Etiquetar contexto previo como relacionado, sin afirmar que el cliente lo citó. | Lógica |
| `backend/app/whatsapp/inbox_routes.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/app/whatsapp/routes.py` | Rechazar cuerpos de webhook mayores de 2 MiB antes de parsearlos. | Validación |
| `backend/app/whatsapp/service.py` | Registrar el rechazo HTTP 429 como fallo reintentable, reutilizar la reserva idempotente solo si el contenido coincide y resolver la alerta tras entrega aceptada. | Lógica/idempotencia |
| `backend/app/workers/jobs_worker.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/scripts/performance_data.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/tests/test_jobs_reliability.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/tests/test_security_config.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/tests/test_settings_email_autoconfig.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/tests/test_whatsapp_agent_sessions.py` | Regresión de contexto conversacional y ciclo de fallo 429/reintento sin reclasificar ni duplicar. | Test |
| `backend/tests/test_whatsapp_auto_responses.py` | Probar reserva única, reintento seguro 429 y no reintento ante resultado ambiguo. | Test |
| `backend/tests/test_whatsapp_conversation_semantics.py` | Ajustar la expectativa del contexto relacionado, sin atribuirlo al cliente. | Test |
| `backend/tests/test_whatsapp_inbox.py` | Ya estaba modificado al comenzar esta revisión; se preservó sin cambios en esta continuación. | Preexistente |
| `backend/tests/test_whatsapp_integration.py` | Probar webhooks, pedidos multiparte/PDF y que el worker reintente fallos 429 en vez de marcar el job como éxito. | Test |
| `docs/development/whatsapp-conversational-agent-delivery.md` | Actualizar resultados finales, inventario del worktree y alcance del interruptor. | Documentación |
| `docs/development/whatsapp-conversational-agent-plan.md` | Aclarar que el único control visible es el interruptor por chat. | Documentación |

## 7. Archivos creados

No se crearon archivos nuevos en esta revisión; todos los archivos de la lista anterior ya estaban versionados.

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Sesiones persistidas y finalizadas por jobs. | Sobreviven a reinicios y permiten idempotencia. | Temporizadores en memoria. |
| Reutilizar el pipeline existente solo al cerrar la conversación. | Mantiene evaluación común sin tratar cada mensaje como pedido final. | Pipeline de pedidos duplicado para WhatsApp. |
| Snapshot validado con `force_human_review=True`. | Conserva líneas y evidencia, pero evita autoconfirmación/exportación. | Volver a extraer el transcript completo y aceptar una decisión automática. |
| Coincidencia de cliente por teléfono del tenant y evidencia antes de crear el pedido. | Evita atribución accidental de conversaciones o pedidos. | Confiar en una asignación histórica o en lo que afirme el modelo. |
| Audio sin transcripción/OCR no se interpreta. | No existe todavía un proveedor de transcripción/OCR integrado y no se deben inventar datos. | Generar una respuesta basada en un medio que no se ha leído. |
| Un interruptor por conversación; chats nuevos en vivo empiezan activos. | Mantiene la operación simple y permite pausar un chat concreto. | Selector de modos y paneles de controles por chat. |
| Reutilizar clasificación/extracción de Correo por turno. | Evita duplicar prompts y reglas de extracción; mantiene un solo comportamiento para los canales. | Un prompt WhatsApp que vuelva a clasificar, extraer y redactar en paralelo. |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `.venv\Scripts\python.exe -m compileall -q app\whatsapp\agent.py app\whatsapp\conversation_semantics.py app\whatsapp\routes.py tests\test_whatsapp_agent_sessions.py tests\test_whatsapp_conversation_semantics.py tests\test_whatsapp_integration.py` (desde `backend`) | Correcto. |
| PowerShell: `APP_ENV=test`, `DEBUG=false`, demo bootstrap y workers internos desactivados; `.venv\Scripts\python.exe -m unittest tests.test_whatsapp_agent_sessions tests.test_whatsapp_conversation_semantics tests.test_whatsapp_inbox tests.test_whatsapp_integration tests.test_whatsapp_conversation_orders tests.test_whatsapp_auto_responses tests.test_whatsapp_demo_simulator tests.test_whatsapp_webhook_observability tests.test_jobs_reliability tests.test_messages_and_conversations tests.test_security_config tests.test_settings_email_autoconfig` (desde `backend`) | 290 tests ejecutados; 4 omitidos y el resto pasó, en 353,125 s. Incluye el interruptor simple por chat y la regresión de PDF cuya descarga termina después del mensaje de cierre. |
| Cinco pruebas dirigidas: respuesta de cantidad a aclaración, clasificación contextual, dos límites webhook y recepción normal | 5 aprobadas en 9,23 s. |
| `$env:DEBUG='false'; $env:APP_ENV='test'; py -3.14 -m unittest discover -s tests` (desde `backend`) | 557 ejecutados; 6 fallos, 6 errores y 5 omitidos, en 1102,81 s. El reporte del proceso identifica al menos un fallo preexistente de `test_whatsapp_webhook_observability`, que espera rutas GET en `/webhooks/whatsapp`; esa ruta no forma parte de los archivos modificados. Los 11 fallos/errores restantes no se aislaron individualmente. |
| `$env:DEBUG='false'; $env:APP_ENV='test'; py -3.14 -m pytest tests/test_whatsapp_webhook_observability.py -q` (desde `backend`) | 2 pasaron y 1 falló en la aserción de rutas GET de webhook indicada arriba; test y rutas fuera del alcance de esta entrega. |
| `py -V:Astral\CPython3.13.15 -c "import pytest, fastapi, sqlalchemy"` | No ejecutable localmente: ese Python 3.13 no tiene instaladas las dependencias; CI configura e instala las dependencias en Python 3.13. |
| GitHub Actions de una ejecución anterior | Compilación y `test-postgres` pasaron; `test-sqlite` falló en la suite general. No se consultaron logs restringidos en esta revisión. |
| `git diff --check` | Correcto. Git solo advierte conversiones LF/CRLF habituales en Windows. |
| `.venv\Scripts\python.exe -m py_compile app\whatsapp\agent.py app\whatsapp\service.py` (desde `backend`) | Correcto. |
| `APP_ENV=test`, `DEBUG=false`, workers demo desactivados; batería focalizada de 8 módulos WhatsApp/jobs (desde `backend`) | 241 pasaron, 4 omitidos, 8 subtests pasaron; 385,67 s. Incluye conversaciones sintéticas, webhooks, PDFs, cola y reintento 429. |
| `GET https://anchi-tan.vercel.app/health/live` y `/health/ready` | 200; métricas por instancia serverless, sin identificar el evento concreto. `tenant=false` es esperable en la consulta sin sesión. |
| `npx.cmd --yes vercel@latest whoami` | La CLI informa `Logged out`; no se pudieron leer logs internos ni verificar la revisión exacta desplegada. |

## 10. Tests añadidos o modificados

Se cubren sesión, confirmación ligada al resumen exacto, FAQ fundamentada, medio sin transcripción, pedidos consecutivos, reintentos, identidad y aislamiento entre tenants. Una regresión recorre «el bot pregunta cantidad → el cliente responde “Serán 4”». Otra sigue mensajes de texto, una línea en PDF ya extraída, una corrección y el cierre hasta crear un único pedido en revisión, comprobando el vínculo de cada línea con su mensaje. Una tercera simula que el cliente cierre mientras el PDF sigue descargándose y comprueba que, al terminar, sus líneas también llegan al pedido de revisión. Un test separado descarga un PDF desde el transporte simulado y lo lleva a revisión. La bandeja conserva un único interruptor por chat; no ofrece modos ni controles adicionales. Las pruebas webhook verifican el rechazo temprano de cuerpos mayores de 2 MiB y la recepción normal.

Las pruebas nuevas también atraviesan el puente síncrono hacia el emisor asíncrono: demuestran que el envío ya no lanza `TypeError`, que un 429 conserva una única reserva y reintenta el mismo texto, que el worker marca el trabajo `retrying`, y que un timeout ambiguo no se vuelve a enviar.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| El cliente mantiene una sesión y su mensaje reinicia 12 h. | Implementado localmente | Modelo, job idempotente y pruebas de vencimiento/reinicio. |
| El agente pide aclaración y no da por válido un «sí» aislado. | Implementado localmente | Revisión exacta de mensaje/resumen/revisión en tests. |
| Datos de catálogo y cliente permanecen aislados por empresa. | Implementado localmente | Consultas tenant-scoped y prueba de rechazo de mensaje de otro tenant. |
| Pedido completo termina en el pipeline común para revisión humana. | Implementado localmente | Dos pruebas de integración del worker/pipeline pasaron tras el ajuste. |
| Sin autoconfirmación ni exportación desde WhatsApp. | Implementado localmente | `force_human_review=True` y flujo final. |
| Toma humana/trigger obsoleto impide respuesta automática. | Implementado localmente | Pruebas de guardas; revalidación antes del envío. |
| Un interruptor por chat activa/desactiva el bot, sin selector de modo. | Implementado y probado localmente | Test de bandeja verifica un único `role="switch"`, ambos estados y aislamiento entre chats. |
| Respuesta breve a pregunta previa conserva contexto y evidencia correcta. | Implementado y probado localmente | Test completo del turno: contexto al clasificador y referencias ligadas a mensajes entrantes. |
| Pedido en varios mensajes con PDF, corrección y cierre crea un único pedido de revisión, incluso si el PDF termina de descargarse después del cierre. | Implementado y probado localmente | `test_multiturn_webhook_text_and_pdf_build_one_review_order` y `test_late_pdf_is_included_when_customer_closes_before_download_finishes`; verifican acumulación y fuente por línea. |
| Webhook sobredimensionado se rechaza antes de parsearlo. | Implementado y probado localmente | Pruebas de streaming y de `Content-Length` en ruta general y por tenant. |
| Un turno clasificado por el pipeline compartido llega al emisor Meta; un fallo no queda como job exitoso. | Corregido y probado localmente; producción pendiente | Regresión de saludo con el puente real simulado, prueba de worker con 429 y código del último commit `c1bb87f` que contenía el defecto de firma. |
| Publicación de la implementación base anterior. | Confirmada históricamente | La entrega anterior registró el commit `b4d827b`; esta revisión local no se ha publicado. |
| Despliegues Vercel asociados a la base anterior `b4d827b`. | Confirmados históricamente | Esta revisión no verificó ni desplegó los cambios actuales. |
| CI del commit de implementación. | Suite SQLite falló; PostgreSQL pasó | Compilación y `test-postgres` verdes; `test-sqlite` falló. El API de logs respondió 403 por falta de permisos admin; no se atribuye el fallo a WhatsApp sin ese traceback. |
| Interruptor visible en producción, Meta real y PostgreSQL concurrente. | Interruptor ausente en la versión observada; Meta/PostgreSQL pendientes | La pestaña autenticada mostró la cabecera sin interruptor. No se enviaron mensajes ni se cambiaron datos. No hay servicio PostgreSQL de pruebas local. |

## 12. Riesgos y observaciones pendientes


- La transcripción de audio y OCR de imagen requieren una integración aparte; mientras tanto se deriva a revisión humana.
- Si el bot estaba apagado cuando llegó un mensaje, al volver a activarlo no se reprocesa ese mensaje anterior; el switch afecta a los siguientes.
- Falta probar la migración y carreras con PostgreSQL de pruebas. Los bloqueos SQLite no son equivalentes a `FOR UPDATE` en producción.
- El límite de turnos por sesión es una cota operativa, no un tope monetario diario. No habilitar respuestas automáticas sin fijar presupuesto/coste si es requisito de negocio.
- Una sesión en `human_handoff` no la cierra automáticamente el barrido de 12 h; requiere acción humana, para no cerrar silenciosamente una atención tomada por una persona.
- Antes de atender a clientes, probar un número de staging con Meta y destinatarios de prueba consentidos, incluida ventana de 24 h, webhooks, medios y estados de entrega.
- El conjunto global local reportó previamente fallos/errores; no se volvió a ejecutar en esta revisión. La suite focalizada tampoco prueba la calidad del modelo real, Meta ni concurrencia en PostgreSQL.
- La corrección del puente está sin publicar. La CLI Vercel está desconectada y no se pudo verificar qué commit ejecuta producción; si Vercel despliega desde la rama predeterminada, el arreglo requiere llegar a esa rama y completar el despliegue automático antes de que quede corregido allí.

## 13. Desviaciones respecto al alcance inicial

Se avanzó con pruebas sintéticas, sin piloto de Meta ni PostgreSQL. Se derivan medios no interpretables a una persona; no se integró OCR/STT sin proveedor/configuración aprobados. La interfaz conserva únicamente el interruptor por chat, sin modos ni controles extra. El modelo real no se probó.

## 14. Estado final de Git

Rama local `chore/technical-improvement-plan`, HEAD `c1bb87f`. El worktree contiene 24 archivos modificados; los cambios previos se conservaron. Esta continuación modificó archivos del agente/servicio de WhatsApp, pruebas relacionadas y este documento. No se crearon archivos. No hubo commit, push ni despliegue. Los cambios locales no están en el remoto ni se afirma que producción ya los contenga.

## 15. Recomendación para la siguiente fase

Antes de darlo por operativo en producción, publicar esta corrección en la rama de despliegue y confirmar el despliegue/logs de Vercel; después validar con un número Meta de staging y destinatarios de prueba consentidos, y verificar migraciones/concurrencia en PostgreSQL. Mantener la interfaz con el único interruptor por chat; OCR/STT solo se añadirán si se aprueban proveedor, retención y coste.
