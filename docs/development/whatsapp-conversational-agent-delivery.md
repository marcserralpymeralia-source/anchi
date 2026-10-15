# Entrega de fase

## 1. Objetivo

Implementar una capa conversacional de WhatsApp que reutilice el runtime de IA y el pipeline de pedidos compartido con Correo, resuelva consultas únicamente con datos aprobados, reúna y aclare los datos del pedido y envíe la conversación a revisión humana tras 12 horas sin mensajes nuevos del cliente.

## 2. Alcance ejecutado

- Sesiones persistentes de pedido por empresa y conversación, con revisiones, corte entre pedidos, vencimiento reiniciable por mensajes del cliente y finalización idempotente mediante jobs.
- Respuestas fundamentadas en FAQ configuradas y catálogo del tenant; identidad asociada solo si el teléfono coincide; cantidades, unidades, referencias y líneas vinculadas a evidencia de mensajes.
- Borrador y preguntas de aclaración deterministas. Una respuesta breve como «sí» solo confirma el mismo resumen de pedido enviado por el agente para la revisión actual.
- Cada turno usa los métodos de clasificación y extracción del pipeline compartido con Correo; no existe una clasificación conversacional paralela en la ruta activa de WhatsApp.
- Envío automático desactivado por defecto, modo sugerencias, límites configurables de turnos y debounce, y toma/devolución del control humano.
- Interruptor persistente y accesible por conversación para alternar entre la IA y la atención humana, también desde estados de derivación o pausa.
- Revalidación justo antes de enviar para cancelar una respuesta si el chat se ha asignado a una persona, el bot se ha desactivado o ya llegó otro mensaje del cliente.
- Al vencer el plazo: consultas sin pedido se cierran sin crear pedido; pedidos incompletos o de identidad no verificada quedan para atención humana; pedidos completos usan un snapshot validado y el pipeline común, siempre con revisión humana y sin confirmar ni exportar automáticamente.
- Medios descargándose quedan pendientes y se reintentan de forma acotada. Audio sin transcripción e imagen sin OCR se derivan a una persona; el bot no finge haber leído su contenido.
- Migración de esquema, ajustes por tenant, presentación del estado en el inbox, registro operativo sin contenido del mensaje y pruebas con datos sintéticos.

## 3. Alcance no ejecutado

- No se envió ni recibió ningún mensaje contra Meta/WhatsApp real; no se usaron datos ni secretos de producción.
- No se ejecutó una migración sobre Neon/PostgreSQL. El código se subió a la rama por defecto; el estado de CI y Vercel se registra en la sección 14.
- No hay OCR ni transcripción de audio integrados en este cambio. Si el medio no aporta texto interpretable tras la descarga, se deriva a revisión humana.
- No se añadió un tope monetario diario específico de WhatsApp ni horarios de atención humana. La protección implementada limita los turnos por sesión; el límite monetario global existente no se amplió en esta fase.
- No se probó concurrencia real de PostgreSQL. Las pruebas usan SQLite y verifican las ramas de tenant, idempotencia y control humano, pero SQLite no reproduce los bloqueos `FOR UPDATE` de PostgreSQL.
- La conexión al navegador integrado no estuvo disponible en esta sesión; la interfaz se validó mediante la respuesta renderizada de la aplicación en los tests de bandeja, no con una sesión visual real.
- Se hizo únicamente un GET público y sin autenticación a `/login` en producción (HTTP 200); no se consultaron chats ni se modificó información de producción.

## 4. Diagnóstico previo

WhatsApp ya persistía mensajes y podía derivarlos al procesamiento de pedidos, pero no tenía una sesión de negocio fiable ni un cierre de 12 horas reiniciable. El flujo anterior podía lanzar la evaluación final ante frases de cierre o un «sí» antes de resolver dudas. El runtime compartido de Correo sí ofrecía clasificación, evaluación y revisión, por lo que se reutiliza al final mediante un snapshot conversacional validado.

La evaluación turno a turno descrita en el diagnóstico corresponde al estado anterior. En la implementación actual, el clasificador y extractor son los mismos métodos de `UnifiedOrderPipelineService` que usa Correo; solo la orquestación de diálogo sigue siendo específica de WhatsApp.

## 5. Cambios realizados

En esta revisión se eliminó el evaluador conversacional propio de los turnos activos de WhatsApp. El adaptador llama a `UnifiedOrderPipelineService.classify_inbound_text` y `extract_inbound_order`, que delegan en los mismos `_classify` y `_extract` usados por Correo. La clasificación se aplica al último mensaje del cliente y la extracción al historial de mensajes entrantes; el texto de salida de la empresa no se mezcla como evidencia del pedido. WhatsApp conserva su gestión de sesión, el borrador conversacional, las respuestas fundamentadas y el cierre tras 12 horas.

Se incorporó el orquestador `backend/app/whatsapp/agent.py`, el modelo/migración de sesión y su integración con jobs y pipeline común. Las consultas de producto/FAQ se limitan a evidencia del tenant; los mensajes recientes se conservan en un contexto acotado; medios no interpretables se derivan; y la finalización verifica revisión, evidencia, identidad y estado humano antes de crear un pedido en revisión. Se añadieron controles/configuración de canal, estados en el inbox y regresiones para el flujo compartido y las carreras de envío.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/agent/platform.py` | Exponer el clasificador y extractor compartidos del pipeline para reutilizarlos por canal. | Lógica |
| `backend/app/agent/prompt_runtime.py` | Eliminar los esquemas y propósitos de prompts WhatsApp que ya no se usan; conservar privacidad mediante `input_reference`. | Lógica/privacidad |
| `backend/app/db/models.py` | Persistir corte de sesión, turnos y reintentos de medios. | Modelo |
| `backend/app/jobs/service.py` | Encolar jobs de agente y vencimiento. | Lógica |
| `backend/app/migrations/registry.py` | Migración compatible de las columnas de sesión. | Migración |
| `backend/app/settings/channels_routes.py` | Guardar límites, debounce, FAQ y configuración del agente. | Ruta/configuración |
| `backend/app/settings/integrations.py` | Exponer valores de configuración del canal. | Integración |
| `backend/app/static/styles.css` | Estilos de estado, sesión y del interruptor IA/atención humana por chat. | UI |
| `backend/app/templates/settings/channels.html` | Controles y ayuda de configuración. | Plantilla |
| `backend/app/templates/whatsapp/_chat_live.html` | Mostrar estado, dudas, borrador y alternancia de control humano por conversación. | Plantilla |
| `backend/app/templates/whatsapp/inbox.html` | Datos de sesión para la bandeja. | Plantilla |
| `backend/app/whatsapp/conversation_semantics.py` | Adaptar la clasificación/extracción compartidas a la sesión y respuestas deterministas de WhatsApp. | Lógica |
| `backend/app/whatsapp/inbox_routes.py` | Acciones de control humano y lectura de sesión. | Ruta |
| `backend/app/whatsapp/service.py` | Debounce, guardas de envío, reserva idempotente y cancelación antes de Meta. | Lógica |
| `backend/app/workers/jobs_worker.py` | Procesar turnos, medios pendientes y finalización. | Worker |
| `backend/tests/test_schema_migrations.py` | Verificar las columnas nuevas. | Tests |
| `backend/tests/test_email_ai_learning.py` | Adaptar los dobles del clasificador/extractor al argumento opcional de referencia que ahora se usa para privacidad y verificar que Correo sigue pasando. | Tests |
| `backend/tests/test_settings_email_autoconfig.py` | Regresión de presentación/configuración. | Tests |
| `backend/tests/test_whatsapp_auto_responses.py` | Idempotencia, propiedad humana, triggers obsoletos y cancelación. | Tests |
| `backend/tests/test_whatsapp_conversation_orders.py` | Regresión del disparador de pedido anterior. | Tests |
| `backend/tests/test_whatsapp_conversation_semantics.py` | Verificar que WhatsApp llama al clasificador/extractor de Correo, conserva privacidad y deriva lo no fundamentado. | Tests |
| `backend/tests/test_whatsapp_inbox.py` | Estado visual del interruptor, toma y devolución del control, y relevo antes de respuesta manual. | Tests |
| `backend/tests/test_whatsapp_integration.py` | Pipeline compartido, asociación de producto y revisión. | Tests |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/whatsapp/agent.py` | Orquestador, grounding, sesiones, timeout, evidencia y handoff. |
| `backend/tests/test_whatsapp_agent_sessions.py` | Cobertura de turnos, pedidos, medios, aislamiento y finalización. |
| `docs/development/whatsapp-conversational-agent-plan.md` | Plan y estado de implementación. |
| `docs/development/whatsapp-conversational-agent-delivery.md` | Esta entrega, según la plantilla del repositorio. |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Sesiones persistidas y finalizadas por jobs. | Sobreviven a reinicios y permiten idempotencia. | Temporizadores en memoria. |
| Reutilizar el pipeline existente solo al cerrar la conversación. | Mantiene evaluación común sin tratar cada mensaje como pedido final. | Pipeline de pedidos duplicado para WhatsApp. |
| Snapshot validado con `force_human_review=True`. | Conserva líneas y evidencia, pero evita autoconfirmación/exportación. | Volver a extraer el transcript completo y aceptar una decisión automática. |
| Coincidencia de cliente por teléfono del tenant y evidencia antes de crear el pedido. | Evita atribución accidental de conversaciones o pedidos. | Confiar en una asignación histórica o en lo que afirme el modelo. |
| Audio sin transcripción/OCR no se interpreta. | No existe todavía un proveedor de transcripción/OCR integrado y no se deben inventar datos. | Generar una respuesta basada en un medio que no se ha leído. |
| Bot desactivado por defecto; control de turnos y modo sugerencias. | Reduce riesgo durante la activación gradual. | Encender envíos automáticos al migrar. |
| Reutilizar clasificación/extracción de Correo por turno. | Evita duplicar prompts y reglas de extracción; mantiene un solo comportamiento para los canales. | Un prompt WhatsApp que vuelva a clasificar, extraer y redactar en paralelo. |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `py -3.14 -m compileall -q app tests` (desde `backend`) | Correcto. |
| `$env:DEBUG='false'; $env:APP_ENV='test'; py -3.14 -m pytest tests/test_whatsapp_agent_sessions.py tests/test_whatsapp_auto_responses.py tests/test_whatsapp_conversation_semantics.py tests/test_whatsapp_integration.py tests/test_whatsapp_conversation_orders.py tests/test_whatsapp_inbox.py tests/test_schema_migrations.py tests/test_email_ai_learning.py -q` (desde `backend`) | 168 pasaron, 4 omitidos, 208 avisos de deprecación, en 313,68 s. Incluye clasificación/extracción compartidas, inbox, toggle, sesiones y migraciones. |
| `$env:DEBUG='false'; $env:APP_ENV='test'; py -3.14 -m unittest discover -s tests` (desde `backend`) | 557 ejecutados; 6 fallos, 6 errores y 5 omitidos, en 1102,81 s. El reporte del proceso identifica al menos un fallo preexistente de `test_whatsapp_webhook_observability`, que espera rutas GET en `/webhooks/whatsapp`; esa ruta no forma parte de los archivos modificados. Los 11 fallos/errores restantes no se aislaron individualmente. |
| `$env:DEBUG='false'; $env:APP_ENV='test'; py -3.14 -m pytest tests/test_whatsapp_webhook_observability.py -q` (desde `backend`) | 2 pasaron y 1 falló en la aserción de rutas GET de webhook indicada arriba; test y rutas fuera del alcance de esta entrega. |
| `py -V:Astral\CPython3.13.15 -c "import pytest, fastapi, sqlalchemy"` | No ejecutable localmente: ese Python 3.13 no tiene instaladas las dependencias; CI configura e instala las dependencias en Python 3.13. |
| `git diff --check` | Correcto. Git solo advierte conversiones LF/CRLF habituales en Windows. |

## 10. Tests añadidos o modificados

Se añadieron casos de sesión, confirmación ligada al resumen exacto, FAQ fundamentada, medio sin transcripción, evidencia de líneas, pedido consecutivo, reintentos de descarga, identidad no verificada y aislamiento entre tenants. Para el adaptador compartido se prueban clasificación de pedido/consulta/incidencia, extracción a borrador, baja confianza sin extracción adicional, respuestas FAQ con evidencia y privacidad de las ejecuciones. La prueba de bandeja verifica el interruptor en ambos estados, junto con los POST de toma y devolución del control.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| El cliente mantiene una sesión y su mensaje reinicia 12 h. | Implementado localmente | Modelo, job idempotente y pruebas de vencimiento/reinicio. |
| El agente pide aclaración y no da por válido un «sí» aislado. | Implementado localmente | Revisión exacta de mensaje/resumen/revisión en tests. |
| Datos de catálogo y cliente permanecen aislados por empresa. | Implementado localmente | Consultas tenant-scoped y prueba de rechazo de mensaje de otro tenant. |
| Pedido completo termina en el pipeline común para revisión humana. | Implementado localmente | Dos pruebas de integración del worker/pipeline pasaron tras el ajuste. |
| Sin autoconfirmación ni exportación desde WhatsApp. | Implementado localmente | `force_human_review=True` y flujo final. |
| Toma humana/trigger obsoleto impide respuesta automática. | Implementado localmente | Pruebas de guardas; revalidación antes del envío. |
| Un interruptor por chat alterna control humano y bot, reflejando también una derivación pendiente. | Implementado y probado localmente | Suite focalizada de 168 tests; la prueba de bandeja verifica ambos estados y ambos POST. |
| Publicación en la rama por defecto. | Push confirmado | `origin/chore/technical-improvement-plan` apunta a `b4d827b`. |
| Despliegues Vercel asociados al commit `b4d827b`. | Correcto | GitHub marcó `Vercel – anchi` y `Vercel – anchi-gemavi` como `success`; un GET sin autenticación a `https://anchi-tan.vercel.app/login` respondió HTTP 200. |
| Prueba visual autenticada en producción, Meta real y PostgreSQL concurrente. | Pendiente | No se accedió a datos de producción ni se enviaron mensajes reales; tampoco hay servicio PostgreSQL de pruebas local. |

## 12. Riesgos y observaciones pendientes


- La transcripción de audio y OCR de imagen requieren una integración aparte; mientras tanto se deriva a revisión humana.
- Falta probar la migración y carreras con PostgreSQL de pruebas. Los bloqueos SQLite no son equivalentes a `FOR UPDATE` en producción.
- El límite de turnos por sesión es una cota operativa, no un tope monetario diario. No habilitar respuestas automáticas sin fijar presupuesto/coste si es requisito de negocio.
- Una sesión en `human_handoff` no la cierra automáticamente el barrido de 12 h; requiere acción humana, para no cerrar silenciosamente una atención tomada por una persona.
- Antes de activar el modo automático, probar una cuenta/número de staging con Meta y destinatarios de prueba consentidos, incluida la ventana de 24 h, webhooks, medios y conciliación de estado.
- El conjunto global local no está verde (6 fallos y 6 errores); además del fallo de observabilidad aislado, quedan 11 casos por clasificar. La suite focalizada de esta entrega sí está verde.

## 13. Desviaciones respecto al alcance inicial

Se implementó el alcance conversacional con pruebas sintéticas y se publicó en la rama por defecto, pero no se realizó piloto de Meta ni PostgreSQL. Se eligió derivar medios no interpretables a una persona en lugar de integrar OCR/STT sin proveedor/configuración aprobados. No se añadieron horarios de atención ni límite monetario diario. La interfaz no pudo probarse con navegador integrado en esta sesión.

## 14. Estado final de Git

Rama de trabajo y rama por defecto del remoto: `chore/technical-improvement-plan`. El commit de implementación `b4d827b` está subido y el ref remoto fue verificado. Vercel confirmó los despliegues de `anchi` y `anchi-gemavi`; la ejecución CI de GitHub estaba `in_progress` en la última consulta. La entrega contiene 23 archivos versionados modificados y 4 nuevos enumerados en las secciones 6 y 7; este informe se añadirá en un commit documental separado. No se cambiaron ramas ni se descartaron cambios locales. Todo el diff corresponde al agente conversacional, su seguridad, interfaz, pruebas o documentación.

## 15. Recomendación para la siguiente fase

Investigar los 11 fallos/errores de la suite general restante y revisar el resultado CI (que corre Python 3.13 y PostgreSQL). Después validar migración y concurrencia con PostgreSQL de pruebas. Para el piloto, conectar un número Meta de staging, completar OCR/STT si el negocio quiere automatizar medios y acordar presupuesto máximo antes de habilitar envíos automáticos en una empresa.
