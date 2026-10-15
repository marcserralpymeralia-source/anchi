# Entrega de fase

## 1. Objetivo

Permitir que el agente de WhatsApp reúna pedidos enviados en varios mensajes y adjuntos, reutilizando el clasificador y extractor compartidos con Correo, sin perder líneas ni confirmar un pedido a partir de contenido incompleto.

## 2. Alcance ejecutado

- Se conserva el contexto de los mensajes consecutivos aunque el worker omita tareas antiguas por haber llegado un turno más reciente.
- Las líneas nuevas se acumulan al borrador existente; las coincidencias actualizan cantidades y otros datos, mientras las líneas anteriores no repetidas se conservan en su orden.
- Las respuestas breves a datos pendientes (por ejemplo, cantidad, referencia o fecha de entrega) pueden continuar el pedido aunque el clasificador compartido las etiquete como fragmento de baja confianza. Un saludo aislado no se interpreta como detalle de pedido.
- El texto extraído de documentos se incorpora al transcript completo hasta un límite seguro de 15.000 caracteres y 50 mensajes. No se recorta cada mensaje a 1.600 caracteres.
- La cola puede volver a procesar el mensaje cuando el estado de descarga/extracción del adjunto cambia. Si el PDF de un turno anterior acaba de descargarse y el turno más reciente estaba esperando, se reevalúa ese turno reciente junto con el PDF.
- Los contextos que superan el límite seguro y los pedidos de más de 30 líneas pasan a revisión humana, en vez de analizar solo una parte y omitir líneas.
- Se revisó el control existente de atención humana por conversación: la ruta y el interruptor permiten pausar el agente para un chat y reanudarlo después. No requirió cambios en esta entrega.

## 3. Alcance no ejecutado

- No se añadió OCR para PDF escaneados o imágenes. Si no se puede extraer texto, el adjunto se deriva a revisión humana.
- No se probó una conversación real contra Meta ni se enviaron mensajes externos.
- No se validó visualmente la interfaz en navegador: la sesión de navegador no estuvo disponible durante esta ejecución.
- No se usaron datos de producción para las pruebas.

## 4. Diagnóstico previo

La sesión empezaba en la hora del último mensaje; si varios mensajes llegaban antes de que el worker procesara el primero, las tareas antiguas se omitían y el transcript podía perder el inicio del pedido. Además, cada mensaje se recortaba a 1.600 caracteres y las extracciones posteriores podían sustituir un borrador por una extracción parcial. Por último, la deduplicación del job de un adjunto no distinguía entre el estado pendiente y el ya extraído, de modo que el turno más reciente podía quedarse esperando aunque el PDF terminase de descargarse.

## 5. Cambios realizados

- Se calcula el inicio de una nueva sesión retrocediendo por los mensajes entrantes contiguos, respetando el timeout configurado y el límite de la sesión anterior.
- El adaptador de WhatsApp sigue usando `UnifiedOrderPipelineService.classify_inbound_text` y `extract_inbound_order`, los mismos métodos empleados por el pipeline de Correo.
- Se fusionan las líneas y notas extraídas con el borrador guardado: una corrección actualiza la línea identificada sin borrar campos que la nueva extracción no incluya; las nuevas líneas se añaden al final.
- Se reconocen respuestas contextuales breves a los datos solicitados, incluidas fechas de entrega, sin convertir saludos o confirmaciones genéricas en líneas de pedido.
- Se amplía el transcript a 15.000 caracteres/50 mensajes y se conserva el comienzo de un documento largo. Si el pedido excede ese contexto, se deriva completo al equipo.
- La clave de deduplicación de un mensaje con adjuntos cambia cuando cambia el estado de extracción, lo que permite reintentar el procesamiento tras la descarga del PDF.
- Se deriva a revisión humana cualquier pedido que exceda 30 líneas; así no se crea un pedido silenciosamente incompleto por el límite del extractor.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/whatsapp/agent.py` | Reconstruir el contexto inicial, reintentar el turno después de la extracción y proteger contextos/pedidos extensos | Lógica de sesión y procesamiento |
| `backend/app/whatsapp/conversation_semantics.py` | Continuar pedidos con fragmentos y fusionar líneas sin perder el orden | Lógica conversacional compartida |
| `backend/app/whatsapp/service.py` | Reencolar mensajes cuando el estado del adjunto cambia | Deduplicación de trabajos |
| `backend/tests/test_whatsapp_agent_sessions.py` | Cubrir mensajes rápidos, PDF, turnos pendientes, contextos extensos y control humano | Tests de sesión |
| `backend/tests/test_whatsapp_conversation_semantics.py` | Cubrir seguimiento contextual, fusión/corrección, saludos y límite de líneas | Tests de semántica |
| `backend/tests/test_whatsapp_integration.py` | Cubrir la reencolación tras extraer media | Test de integración |
| `backend/tests/test_messages_and_conversations.py` | Mantener el mock de creación de pedidos alineado con los argumentos actuales del pipeline | Test de confirmación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/development/whatsapp-multi-message-orders-delivery.md` | Registrar alcance, decisiones, validación y límites de esta entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Reutilizar el pipeline compartido de Correo | Mantener una clasificación y extracción coherentes entre canales | Crear un segundo clasificador o prompt para WhatsApp |
| Mantener un borrador estructurado entre turnos | Un cliente puede enviar producto, cantidad, corrección y unidad en mensajes distintos | Sustituir el borrador con cada extracción aislada |
| Reprocesar el turno reciente cuando un adjunto anterior termina de extraerse | La descarga del documento puede terminar después de mensajes posteriores | Dar por definitivo un turno evaluado mientras el PDF estaba pendiente |
| Derivar documentos extensos o de más de 30 líneas a revisión humana | Evitar crear pedidos con líneas omitidas | Truncar o aumentar sin límite el contexto automático |

## 9. Validaciones ejecutadas

| Comando o comprobación | Resultado |
|---|---|
| `py -3.14 -m pytest tests/test_whatsapp_conversation_semantics.py tests/test_whatsapp_agent_sessions.py tests/test_whatsapp_conversation_orders.py tests/test_whatsapp_auto_responses.py tests/test_whatsapp_inbox.py tests/test_whatsapp_integration.py -q` (con `DEBUG=false` solo en el proceso de pruebas) | 131 aprobadas, 4 omitidas; sin fallos |
| `py -3.14 -m pytest tests/test_whatsapp_conversation_semantics.py tests/test_whatsapp_agent_sessions.py tests/test_whatsapp_integration.py -k 'not inbox' -q` (con `DEBUG=false` solo en el proceso de pruebas) | 86 aprobadas; sin fallos, tras las últimas salvaguardas |
| `.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_whatsapp*.py' -q` (FastAPI 0.115.6, `APP_ENV=test`, `DEBUG=false`) | 141 correctas, 4 omitidas |
| `.venv\Scripts\python.exe -m unittest tests.test_messages_and_conversations -q` | 15 correctas tras alinear el mock con la firma del pipeline |
| `.venv\Scripts\python.exe -m unittest tests.test_manual_whatsapp_import tests.test_agent_parsing_regressions tests.test_messages_and_conversations tests.test_jobs_reliability tests.test_schema_migrations -q` | 63 correctas |
| `.venv\Scripts\python.exe -m unittest discover -s tests -q` | 568 tests; 5 fallos y 5 omitidos. Fallan cuatro pruebas de `test_multitenancy_auth` y una de adjuntos en `test_whatsapp_inbox`; las cinco pasan al ejecutar sus módulos/casos aislados y los 141 tests WhatsApp pasan juntos. Indicio de contaminación/orden en la suite global; no se cuenta como suite verde |
| `.venv\Scripts\python.exe -m compileall -q app scripts tests` | Correcto |
| `git diff --check` | Correcto tras la revisión final |
| `curl.exe --silent --show-error --location --max-time 25 --output NUL --write-out "anchi-tan HTTP %{http_code}\n" https://anchi-tan.vercel.app/login` y equivalente para `https://anchi-gemavi.vercel.app/login` | Ambos dominios públicos responden HTTP 200; comprobación no autenticada, no demuestra el flujo de WhatsApp |
| Estado de GitHub Actions | No se pudo leer el estado actual: `gh` no está instalado y la API pública devolvió 403 por límite de solicitudes |
| Prueba visual autenticada y conversación real con Meta | No ejecutadas: el navegador integrado no conectó y no se usaron datos ni contactos de producción |

La primera ejecución local no llegó a recopilar tests porque el entorno tenía un valor no booleano para `DEBUG`; se repitieron sin cambiar archivos ni secretos, fijando `DEBUG=false` solo para los procesos de prueba. Las suites informaron avisos deprecados de SQLite/SQLAlchemy y del cliente de pruebas Starlette; no afectan el resultado de aprobación.

## 10. Tests añadidos o modificados

- El transcript conserva mensajes rápidos y el texto de PDF superior a 1.600 caracteres, excluyendo mensajes de sesiones antiguas.
- Una respuesta con cantidad/unidad continúa el pedido de baja confianza, conserva las líneas previas y mantiene su orden.
- Una corrección actualiza la cantidad de la línea correspondiente sin borrar referencia, unidad o notas.
- El texto extraído de un PDF anterior puede reactivar el turno más reciente que esperaba ese adjunto.
- Un cambio de estado del adjunto permite encolar un nuevo job de procesamiento.
- Saludos aislados no disparan una extracción y las respuestas de fecha de entrega se reconocen como contextuales.
- Un PDF demasiado extenso o un pedido de más de 30 líneas se deriva a revisión humana en vez de finalizar parcialmente.
- El test de takeover confirma que el agente queda silenciado para el chat hasta que se reanuda.
- El mock de `_create_order` acepta `fast_path` y `whatsapp_session_id`, como la implementación real del pipeline.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Un pedido repartido en mensajes consecutivos conserva su contexto | Cumplido | Tests de sesión con mensajes rápidos |
| Los adjuntos PDF con texto llegan al pipeline compartido | Cumplido para extracción disponible | Tests de transcript y descarga; no se validó Meta en vivo |
| Los fragmentos de baja confianza no reemplazan ni descartan el borrador | Cumplido | Tests de seguimiento contextual y fusión |
| Una descarga tardía no deja el turno bloqueado | Cumplido | Tests de reencolación y reintento |
| El control humano pausa/reanuda el agente por conversación | Cumplido en código existente | Ruta/formulario y test de takeover |
| Los documentos o pedidos que exceden límites no generan un pedido truncado | Cumplido | Tests de contexto extenso y más de 30 líneas |

## 12. Riesgos y observaciones pendientes

- Los PDF escaneados requieren OCR; mientras no esté implementado, necesitan revisión humana.
- Conversaciones de más de 50 mensajes, contexto superior a 15.000 caracteres o pedidos con más de 30 líneas requieren revisión completa.
- Las pruebas usan SQLite, proveedores simulados y datos sintéticos; no prueban una cuenta Meta real ni la recepción/envío en WhatsApp de producción.
- No se pudo comprobar visualmente la conversación autenticada porque el navegador de esta sesión no estuvo disponible. La respuesta HTTP 200 solo confirma que la página pública de acceso está publicada.
- La suite global todavía no está verde: los cinco fallos solo aparecen al ejecutar todos los módulos juntos; los cuatro tests de autenticación y el test de adjunto pasan aislados, y los 141 tests WhatsApp pasan juntos. Hay que aislar y corregir la contaminación de estado antes de usar la suite global como gate fiable.
- El navegador integrado no conectó y no se enviaron mensajes reales a Meta. Los GET públicos HTTP 200 solo prueban disponibilidad de la pantalla de login, no el flujo autenticado ni la recepción/envío de WhatsApp.

## 13. Desviaciones respecto al alcance inicial

Se añadió una salvaguarda explícita para pedidos extensos y se documentaron los límites de OCR. No se implementó extracción por lotes: habría que diseñarla con deduplicación entre páginas y trazabilidad por línea para evitar duplicados u omisiones.

## 14. Estado final de Git

- Rama: `chore/technical-improvement-plan` (rama predeterminada confirmada en `origin`).
- Commit de implementación: `f35c1b0` (`fix: preserve WhatsApp orders across messages and PDFs`), enviado a `origin/chore/technical-improvement-plan`.
- El commit de implementación `f35c1b0` ya estaba en la rama predeterminada. En esta continuación se alineó un mock de test y se actualizó este informe con la verificación ampliada; el estado final del repositorio se comprueba al cerrar la entrega.

## 15. Recomendación para la siguiente fase

Validar en un entorno de pruebas con pedidos sintéticos de varios turnos y PDFs con texto, incluyendo correcciones, varias líneas y respuestas a preguntas del agente. Después diseñar OCR y extracción por lotes con trazabilidad y deduplicación antes de ampliar los límites.
