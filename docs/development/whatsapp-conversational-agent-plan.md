# Plan: agente conversacional de WhatsApp y cierre de pedidos tras 12 horas

Estado: **implementada localmente, pendiente de validación final y despliegue**. Alcance: conversaciones entrantes de WhatsApp Cloud API por empresa. La validación se hizo con datos sintéticos locales; no se enviaron mensajes ni se usaron datos de producción.

## Resultado de la implementación local

- Se reutiliza el runtime de IA compartido y el pipeline final de pedidos, alimentándolo con un borrador validado y evidencia de mensajes; el paso por el pipeline fuerza revisión humana.
- El plazo predeterminado es de 12 horas desde el último mensaje entrante del cliente y se reinicia con cada nuevo turno suyo. Las conversaciones se cierran con jobs persistentes e idempotentes.
- El bot queda desactivado por defecto; se ofrecen modo sugerencias, FAQ, debounce y límites por sesión. Las respuestas automáticas se vuelven a validar justo antes de llamar a Meta.
- Dudas sin evidencia aprobada, identidad no verificada, turnos agotados o medios no interpretables se derivan a revisión humana.
- Una confirmación corta solo es válida para el resumen exacto y la revisión actual del borrador.
- No se integraron proveedores OCR/STT, límite monetario diario específico ni horario de atención humana. Son evoluciones pendientes; los medios no interpretables se derivan y el límite de turnos por sesión es la protección de coste incluida.
- No se probó con Meta, datos de producción ni PostgreSQL real. El detalle y los comandos están en `whatsapp-conversational-agent-delivery.md`.

### Ajuste de arquitectura de la IA conversacional

La implementacion del turno de WhatsApp reutiliza los metodos compartidos del pipeline de Correo: `UnifiedOrderPipelineService._classify` y `_extract`, expuestos mediante `classify_inbound_text` y `extract_inbound_order`. Cada turno clasifica el ultimo mensaje entrante del cliente; cuando corresponde, la extraccion recibe el historial acotado de mensajes del cliente y el mensaje de WhatsApp para conservar el tipo de origen. No se envia al modelo el texto generado por la empresa como si fuera contenido del cliente.

La parte especifica de WhatsApp se limita a mantener la sesion, conservar el borrador, decidir cuando preguntar/derivar y responder con FAQ o catalogo verificados. Las respuestas no las redacta un segundo prompt conversacional. El cierre por inactividad de 12 horas y la revision humana al finalizar se mantienen.

## Objetivo y regla principal

Reutilizar la configuración de IA y el pipeline de evaluación de pedidos que ya utiliza el correo, pero añadir antes una capa conversacional específica de WhatsApp. El bot debe entender la intención, contestar dudas cuando disponga de datos fiables, recopilar los campos necesarios del pedido y pedir aclaraciones concretas. Un pedido no debe pasar al pipeline definitivo por el simple hecho de que la IA lo considere completo.

**Propuesta operativa:** las 12 horas se cuentan desde el **último mensaje entrante del cliente** perteneciente a la sesión de pedido. Un nuevo mensaje del cliente reinicia el plazo; una respuesta del bot o de un agente no lo reinicia. Al vencer, se congela una versión de la sesión y se envía a evaluación si existe intención de pedido. Si falta una respuesta imprescindible, se cierra como *incompleto para revisión humana*: no se inventan datos ni se confirma/exporta automáticamente. La solicitud expresa de cierre por el cliente permite adelantar la evaluación, pero solo tras comprobar los datos imprescindibles. Las consultas sin pedido se cierran sin crear pedido.

Las 12 horas son una regla interna de negocio, **distinta** de la ventana de atención de 24 horas de Meta. Fuera de esta última, no se envían respuestas libres ni recordatorios automáticos; únicamente se podría usar una plantilla aprobada si en el futuro se decide habilitarla. Meta exige además una vía clara de escalado humano para la automatización. Fuente: [WhatsApp Business Messaging Policy](https://whatsappbusiness.com/policy/).

## Diagnóstico del código actual

| Componente | Situación actual | Consecuencia para el objetivo |
|---|---|---|
| Correo | `backend/app/settings/integrations.py` encola `process_email`; `backend/app/workers/jobs_worker.py` llama a `AgentProcessingService.process_email_fast`; `backend/app/agent/platform.py` clasifica, extrae, asocia cliente/producto, puntúa y abre revisión o autoconfirma. | Reutilizar esta evaluación **al final**, no en cada turno. |
| Entrada WhatsApp | `backend/app/whatsapp/routes.py` y `backend/app/whatsapp/service.py` persisten mensajes y adjuntos; el worker descarga medios y encola `process_inbound_message`. | Se pueden conservar webhook, almacenamiento, cola, deduplicación y transporte de Meta. |
| IA conversacional | La ruta activa de `backend/app/whatsapp/conversation_semantics.py` delega clasificación y extracción en `UnifiedOrderPipelineService`, usando los mismos `_classify` y `_extract` que Correo. | La sesión conserva el estado conversacional; FAQ, catálogo, respuesta y revisión siguen siendo responsabilidades específicas del canal. |
| Activación del pedido | `backend/app/whatsapp/conversation_orders.py` considera listo un pedido con ciertas frases de cierre o un «sí» contextual y construye un transcript de hasta 40 mensajes desde la fecha del último pedido. El worker llama entonces a `UnifiedOrderPipelineService`. | Una frase de cierre puede adelantar el pipeline sin resolver ambigüedades. El corte por fecha del último pedido y el máximo de mensajes no representan una sesión de negocio fiable. |
| Respuestas automáticas | `send_automatic_response` guarda la respuesta, tiene idempotencia por mensaje y comprueba la ventana de envío. `max_auto_messages` vale 3 por defecto y el recuento se reinicia respecto al último pedido. | Mantener garantías de envío, pero sustituir el límite rígido por límites por sesión/ritmo y un modo de intervención humana. |
| Medios | La descarga y extracción son asíncronas; audio puede quedar `transcription_pending` y las imágenes almacenadas no aportan necesariamente texto evaluable. | No cerrar ni responder sobre contenido de un adjunto aún no interpretable. Traspasar a una persona cuando no se pueda interpretar. |
| Revisión/autoconfirmación | El pipeline puede confirmar automáticamente si `LLMSettings.allow_auto_confirm`, las reglas de decisión y el scoring lo permiten; el veto específico `EmailSettings.always_human_review` solo se aplica al correo. | «Enviar a evaluar» no significa «confirmar». Primera versión: revisión humana obligatoria para WhatsApp y bloqueo de exportación hasta confirmación; más adelante, política explícita por canal. |
| Planificación de trabajos | `vercel.json` ejecuta `/cron/jobs` cada minuto; el worker recorre empresas activas y procesa jobs. | Añadir barrido de sesiones vencidas por empresa con bloqueo e idempotencia. No depender de temporizadores en memoria o de una función Vercel durmiendo 12 horas. |

## Contrato funcional propuesto

1. **Una conversación puede contener varias sesiones de pedido.** La conversación es el hilo telefónico; la sesión es una propuesta concreta, con inicio, fin, versión y pedido asociado. Se permiten consultas sin pedido y varios pedidos consecutivos sin mezclar sus líneas.
2. **Identidad del cliente.** Relacionar el número normalizado con contactos del mismo `company_id`; si no existe coincidencia única, no asumir identidad ni divulgar datos de otro cliente. Recoger solo datos necesarios o derivar a revisión.
3. **Respuesta fundamentada.** El bot puede usar productos y datos del cliente autorizados para ese tenant, más respuestas empresariales aprobadas que se incorporen a la configuración. El modelo no inventa precio, disponibilidad, plazo, condiciones comerciales ni referencias. El catálogo actual tiene `sale_price`, pero no una fuente fiable de stock: no prometer existencias.
4. **Pedido como borrador estructurado.** Guardar líneas, cantidades, unidades, variante/referencia y dudas pendientes con referencias a los mensajes fuente. El modelo propone; validaciones deterministas contrastan que cada dato exista y sea compatible con el catálogo. Cambios del cliente actualizan el borrador, sin duplicar líneas por reintentos.
5. **Preguntas necesarias y breves.** Una pregunta clara por turno o grupo natural de dudas; no repetir preguntas ya contestadas. Si la pregunta exige un dato ausente de la base, expresar la limitación y ofrecer derivación humana. El bot debe poder responder preguntas intercaladas sin perder el pedido.
6. **Control humano.** Acción visible para pausar/reanudar el bot por conversación, tomar el control y volverlo a ceder; si un empleado responde, el bot no compite con él. Solicitudes de «persona», quejas, contradicciones, baja confianza, identidad incierta, errores repetidos y productos no verificables provocan derivación.
7. **Cierre.** El cliente puede pedir terminar antes de las 12 horas. En ausencia de cierre expreso, a las 12 horas desde su último mensaje se evalúa. El bot puede haber preguntado antes; si no hubo respuesta, el cierre queda incompleto y revisable. No se manda un mensaje final automático si no es necesario, especialmente si la ventana de Meta está cerrada.
8. **Aislamiento.** Todos los accesos a sesión, contactos, productos, prompts, trabajos y pedidos se filtran por `company_id`. El bot de una empresa nunca recibe el contexto de otra.

## Estados y transiciones

| Estado de sesión | Evento | Acción |
|---|---|---|
| `collecting` | Mensaje entrante | Actualizar borrador y plazo; contestar o preguntar si procede. |
| `awaiting_customer` | El bot solicita un dato esencial | Esperar; otro mensaje del cliente vuelve a evaluación y reinicia las 12 h. |
| `ready_for_review` | Pedido suficientemente completo | Mantener el borrador abierto hasta cierre expreso o inactividad; mostrar resumen/solicitar confirmación si corresponde. |
| `human_handoff` | Escalado o toma de control | No enviar respuestas automáticas; mantener visibilidad y temporizador conforme a la decisión operativa. |
| `finalizing` | Cierre expreso válido o vencimiento | Bloquear sesión, releer mensajes/versión/medios pendientes y producir un único snapshot inmutable. |
| `closed_review` | Pedido completo o parcial con intención de compra | Invocar evaluación existente sobre el snapshot **una vez**; crear revisión obligatoria si corresponde. |
| `closed_no_order` | Solo consulta u otra intención | No crear pedido; conservar trazabilidad. |
| `failed_review` | Fallo de IA, adjunto sin interpretar o pipeline fallido | Reintento seguro o cola humana; nunca descartar la sesión en silencio. |

El estado de «pedido completo» no equivale a «pedido confirmado», y «cerrado» tampoco equivale a «enviado al cliente» o «exportado».

## Diseño técnico propuesto

### Persistencia

- Introducir una entidad `WhatsAppOrderSession` por `company_id` y `conversation_id`, con identificador/secuencia, estado, `started_at`, `last_customer_message_at`, `due_at`, `revision`, `last_processed_message_id`, `draft_json` validado, dudas, motivo de cierre, snapshot final, `order_id`, timestamps de cierre y modo bot/humano. Índices para sesiones activas y `due_at`.
- Garantizar a nivel de base de datos una sola sesión activa por conversación y una clave única de finalización por sesión/revisión. Acordar la migración compatible con SQLite de pruebas y PostgreSQL de producción, usando el sistema de migraciones existente.
- No guardar en logs el contenido completo de mensajes, prompts, teléfonos o secretos; usar IDs, resultados, duraciones y códigos de error. Los registros de ejecuciones de prompt existentes requieren revisar la política de retención/redacción antes de ampliar el volumen de conversación.
- Mantener el transcript visible completo en `InboundMessage`; construir para IA un contexto acotado: resumen estructurado validado + últimos turnos relevantes + referencias de mensajes, sin perder líneas anteriores al superar 40 mensajes o el límite de tokens.

### Procesamiento de cada turno

1. Verificar webhook de Meta, deduplicar por ID de mensaje, persistir y responder rápido a Meta; procesar en job por empresa/conversación.
2. Esperar a que el medio imprescindible esté descargado y transcrito/extraído. Imagen sin OCR o audio sin transcripción: pedir texto o derivar, sin fingir haberlo leído.
3. Serializar la ejecución por sesión para que mensajes próximos, webhooks repetidos, cron y respuestas manuales no se pisen. Agrupar ráfagas breves de mensajes del cliente antes de responder para evitar tres respuestas a tres fragmentos de un mismo pedido.
4. Clasificar el último mensaje entrante con el clasificador común de Correo. Si es un pedido o una continuación, pasar solo los mensajes del cliente de la sesión al extractor común; WhatsApp conserva el borrador y aplica reglas deterministas para aclaraciones, envío, catálogo y toma humana. Las respuestas se limitan a hechos autorizados de FAQ/catálogo; no hay un segundo prompt que vuelva a clasificar, extraer y redactar. No dar al modelo acceso SQL arbitrario.
5. Verificar que el mensaje entrante sigue siendo el último turno esperado y que el bot sigue habilitado/propietario del chat. Enviar con la infraestructura de idempotencia existente, conservar estados `sending`/`accepted`/`failed`/`send_unknown` y conciliar con estados webhook. No reintentar ciegamente un envío de resultado desconocido.

### Vencimiento a 12 horas

- El cron consulta sesiones activas con `due_at <= now()` en UTC y encola finalizaciones idempotentes; debe funcionar para todos los tenants activos sin recorrer cada mensaje. Medir retraso entre `due_at` y ejecución.
- El job bloquea la sesión, vuelve a leer `revision`, último mensaje entrante y medios pendientes. Si entró un mensaje más reciente, abandona el cierre antiguo y actualiza `due_at`. Si hay un envío/procesamiento pendiente, reprograma con límite y alerta si se atasca.
- Solo una transición `finalizing` puede producir el snapshot y llamar al pipeline. El snapshot se vincula a los mensajes fuente y al pedido/revisión. Un retry no crea otro pedido ni duplica la notificación.
- Si el cliente vuelve después del cierre, abrir una sesión nueva. Una revisión anterior pendiente se relaciona explícitamente con el nuevo contacto; nunca reescribir un pedido confirmado/exportado de forma implícita.

### Integración con el pipeline de correo

- Reutilizar `LLMSettings` y los métodos de clasificación/extracción del pipeline común en cada turno; al cerrar, reutilizar también matching, scoring y revisión. El orquestador conversacional es una capa nueva; no copiar el procesamiento de correo dentro de WhatsApp.
- Añadir una entrada finalizadora con `session_id` y snapshot estable, no reutilizar sin más el último mensaje como si fuese todo el pedido. Sustituir el disparo actual basado en frases/«sí» por la máquina de estados; una confirmación corta solo vale si referencia un resumen concreto y una versión vigente del borrador.
- Introducir política de decisión por canal. Para el primer despliegue WhatsApp: **siempre revisión humana antes de confirmar/exportar**, incluso si el scoring del pipeline supera el umbral; casos incompletos deben entrar como incidencia/borrador revisable y no como pedido aparentemente válido. Definir cómo se muestran en Pedidos y en el inbox.
- Preguntas generales que no contienen pedido no deben pasar por la creación de pedidos. Si no hay respuesta aprobada, ofrecer atención humana.

### Configuración y experiencia de usuario

- Ajustes por empresa: bot desactivado por defecto al migrar, activar en modo sugerencias/prueba, pasar a respuestas automáticas tras validación; plazo de 12 h configurable con valor inicial 12, pausa global, horario de atención para humanos, instrucciones/FAQ aprobadas y límites de coste/turnos.
- En el chat: distintivo discreto de respuesta automática, estado del borrador y plazo, dudas pendientes, «Tomar control», «Reanudar bot», «Cerrar y evaluar ahora», enlaces a pedido/revisión y motivo de derivación. Evitar exponer internamente el prompt o el razonamiento del modelo al cliente.
- Observabilidad: eventos `turn_received`, `media_pending`, `intent_evaluated`, `reply_skipped/sent`, `handoff`, `timer_reset`, `finalize_started/skipped/completed`, `review_opened`; duración, coste estimado, errores y versiones, sin secretos ni texto sensible en logs.

## Secuencia aplicada en esta fase

1. **Contrato y pruebas de dominio.** Aplicado con fixtures sintéticos para pedidos completos/incompletos, consulta, toma humana, identidad, evidencia y medios.
2. **Persistencia y migración.** Aplicada la entidad de sesión y migración compatible; bot desactivado por defecto.
3. **Orquestador conversacional.** Aplicado con runtime común, consultas tenant-scoped, FAQ/catálogo fundamentados, borrador, debounce, límites y handoff.
4. **Temporizador y finalización.** Aplicados jobs persistentes, revisión de revisión/último mensaje, retry de medios acotado y cierre idempotente.
5. **Pipeline y revisión.** Aplicado el snapshot al pipeline compartido con veto de autoconfirmación/exportación.
6. **Interfaz y despliegue gradual.** Aplicados ajustes, inbox y modo sugerencias. El piloto externo y la operación gradual quedan pendientes.

## Matriz mínima de aceptación

| Escenario | Resultado esperado |
|---|---|
| Cliente aporta producto ambiguo | Una pregunta concreta; sin producto inventado ni pedido finalizado prematuramente. |
| Cliente pregunta algo verificable del catálogo | Respuesta basada en registro de su empresa; no prometer stock si no hay fuente. |
| Cliente envía tres fragmentos seguidos | Un turno lógico y, como máximo, una respuesta pertinente; borrador sin duplicados. |
| Cliente responde 11 h 59 min después | Reinicia 12 h; un job de cierre viejo no finaliza. |
| Cliente guarda silencio 12 h con pedido completo | Un snapshot y una evaluación; revisión humana; sin confirmación/exportación automática. |
| Cliente guarda silencio 12 h con dato imprescindible sin resolver | Cierre incompleto para persona; no pedido falsamente completo; sin insistir por WhatsApp. |
| Cliente dice «confirma» pero faltan cantidad o variante | No se confirma; se pregunta o se deriva. |
| Cliente solo hace una consulta | Respuesta o derivación; ninguna orden. |
| Agente humano toma el chat | Bot silenciado antes de enviar; sus respuestas no reinician el reloj de cliente. |
| Audio sin transcripción o imagen sin texto | No usar contenido supuesto; pedir aclaración o derivar. |
| Dos workers, webhook duplicado o fallo tras enviar | Un solo turno, una sola respuesta y una sola evaluación; estados reconciliables. |
| Mensaje fuera de ventana de Meta | No se envía texto libre; cierre interno y revisión siguen funcionando. |
| Dos empresas con el mismo teléfono/producto | Datos y pedidos siempre separados por tenant. |

Tests relacionados a ampliar: `backend/tests/test_whatsapp_conversation_orders.py`, `test_whatsapp_conversation_semantics.py`, `test_whatsapp_auto_responses.py`, `test_whatsapp_integration.py` y regresiones del pipeline de email/pedidos. Usar Meta y LLM simulados; no utilizar clientes ni credenciales reales. Probar migraciones, carreras de cron/webhook, idempotencia y recuperación de fallos en SQLite y PostgreSQL de pruebas.

## Decisiones confirmadas y pendientes antes de producción

### Decisiones implementadas

1. Un «sí» breve solo confirma la última versión del resumen concreto enviado por el bot; no confirma una respuesta informativa o un borrador distinto.
2. Si el pedido sigue incompleto al vencer el plazo, se cierra para revisión humana; no se inventan cantidades ni se crea un pedido aparentemente válido.
3. Si el teléfono no coincide con un cliente del tenant, se exige verificación humana antes de crear el pedido.
4. Tomar el chat por una persona pausa los envíos automáticos y evita finalizar la sesión por timeout mientras siga en handoff.
5. No se envían recordatorios ni mensajes proactivos al vencer las 12 horas.
6. Cuando el cliente expresa un nuevo pedido de manera explícita, se separa de la sesión anterior; no se mezclan sus líneas.

### Pendientes operativos

- Cada empresa debe revisar y aprobar su FAQ y datos comerciales antes de activar el modo automático.
- Acordar presupuesto/coste máximo diario si se requiere una cota monetaria adicional al límite de turnos por sesión.
- Definir si se necesitan horarios de atención humana, y su zona horaria/reglas.
- Decidir si se quiere integrar STT/OCR y aprobar proveedor, retención y tratamiento de medios.
- Ejecutar piloto en Meta de staging, pruebas de PostgreSQL y revisar eventos de envío/webhook antes de producción.
