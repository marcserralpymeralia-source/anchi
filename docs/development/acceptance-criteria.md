# Criterios de aceptacion

## Globales

- Mantener el aislamiento master/tenant.
- No introducir secretos en codigo.
- No modificar funcionalidades fuera del alcance.
- No añadir dependencias sin justificacion.
- No duplicar logica ya existente.
- Mantener compatibilidad con el flujo principal.

## Seguridad

- No exponer credenciales ni tokens.
- Rechazar configuracion insegura en produccion.
- No permitir secretos demo ni claves debiles en produccion.
- Mantener cookies seguras y hosts/origenes explicitos.
- No usar datos de produccion.
- No ejecutar acciones destructivas.
- Mantener aislamiento estricto entre master y tenant.
- No resolver tenant por `company_id`, slug o host sin membresia valida.
- No permitir acceso master con rol tenant.
- Rechazar sesiones incompletas o inconsistentes.

## Tests

- Ejecutar los tests relacionados antes de cerrar una fase.
- Ejecutar validacion sintactica.
- Ejecutar tests especificos de configuracion segura.
- Mantener request_id y correlation_id en la traza de request, logs y jobs.
- Exponer health/live, health/ready y metricas internas basicas.
- Medir tiempo total, SQL, consultas duplicadas, renderizado Jinja, tamaño de respuesta y volumen de registros en requests criticos.
- Mantener un benchmark reproducible con salida en JSON/CSV y datos sintéticos temporales.
- Mantener home y workbench bajo 12 consultas SQL y 1 consulta duplicada en los escenarios small y medium tras la fase 6B.
- Cubrir ese presupuesto con `backend/tests/test_home_workbench_optimization.py`.
- Mantener el listado general de pedidos bajo 20 consultas SQL y 3 consultas duplicadas en los escenarios small, medium y large tras la fase 6C.
- Mantener `/orders?date_range=90d` por debajo de 500 KB de HTML en scenario medium tras la fase 6C.
- Cubrir ese presupuesto con `backend/tests/test_orders_list_optimization.py`.
- No degradar en mas de un 10% las rutas de control medidas en scenario medium tras una optimizacion de listado.
- Mantener el detalle de pedido bajo 15 consultas SQL, 2 consultas duplicadas, 140 KB de HTML y 108 registros cargados en los escenarios small, medium y large tras la fase 6D.
- Cubrir ese presupuesto con `backend/tests/test_orders_detail_optimization.py`.
- No volver a cargar cliente, producto o adjuntos completos por relaciones lazy dentro de `/orders/{order_id}`.
- Mantener el detalle de pedido basado en snapshots y candidatos compartidos, no en catálogos completos embebidos.
- Mantener `/channels?tab=processed&date_range=30d` por debajo de 20 consultas SQL y 3 consultas duplicadas en los escenarios small, medium y large tras la fase 6E.
- Mantener la bandeja de canales basada en pagina SQL visible, resumen agregado y adjuntos cargados solo para las filas visibles.
- Mantener un contrato comun de mensaje y conversacion con email como adaptador inicial, deduplicacion por tenant/proveedor/external_id y relacion orden-conversacion compatible.
- Mantener una ruta unificada de resolucion para email y WhatsApp/inbound que lleve el usuario al detalle operativo correcto o encole el trabajo necesario sin duplicar logica.
- Mantener la importacion manual de WhatsApp con previsualizacion, proceso de scoring y confirmacion final en el mismo tenant.
- Mantener el detalle de pedido capaz de mostrar correo o chat de conversación en una unica pantalla de revision.
- Preservar la compatibilidad con mensajes y pedidos existentes durante la migracion del nuevo modelo comun.
- Mantener `backend/app/db/models.py` monolitico solo si dividirlo introduce dependencias circulares o demasiado codigo puente; documentar la decision.
- Eliminar solo redirecciones legacy y helpers huérfanos cuando exista sustituto probado y los tests cubran el flujo activo.
- Cubrir el flujo email -> inbound message -> conversation -> processing -> order -> review -> approval -> simulated export -> audit.
- Cubrir el flujo duplicate mail -> no duplicate message -> processing failure -> retry -> valid order -> export failure -> retry -> success.
- Cubrir el flujo manual WhatsApp -> preview -> process -> confirm -> inbound message -> conversation -> processing -> order review.
- No filtrar secretos en logs estructurados ni en trazas persistidas.
- Documentar cualquier fallo que no se corrija en ese momento.

## Compatibilidad

- No romper el arranque local.
- No cambiar rutas ni endpoints salvo que el objetivo lo requiera.
- Mantener la navegacion principal limitada a `Pedidos pendientes`, `Entradas`, `Productos`, `Clientes` y `Conocimiento`.
- Mantener `Configuracion` como area secundaria visible para administradores.
- Mantener `/entries` como ruta canonica para email, WhatsApp e importaciones manuales, sin duplicar entidades ni pipeline.
- Mantener rutas legacy como soporte interno cuando sean necesarias, pero sin exponerlas en la navegacion principal.
- No alterar el modelo de datos salvo instruccion explicita.
- No permitir acceso cruzado entre dos companias con IDs coincidentes en bases distintas.
- Mantener jobs idempotentes, con reintentos limitados, historial de intentos y recovery de jobs bloqueados.
- Garantizar que el worker tenga una entrada CLI estable y que no procese un job antes de su fecha de reintento.
- Mantener el correo IMAP con checkpoints por UID/UIDVALIDITY, deduplicacion y estado de backfill persistido en master.
- Registrar cada ejecucion de prompt en `prompt_executions` y validar la salida estructurada antes de usarla.
- Mantener propuestas de aprendizaje controladas, revisables y separadas del dato operativo.
- Proveer una utilidad de evaluacion reproducible para comparar salidas esperadas y reales del agente sin llamar a IA real en tests.
- No activar profiling en produccion.
- No introducir un segundo sistema paralelo de pedidos al modelar mensajes y conversaciones.

## Migraciones

- No modificar las bases originales durante inventario, baseline o simulacion.
- Ejecutar `dry-run` sin escribir tablas, columnas, indices, filas ni ledger.
- Baselinear solo esquemas reconocibles y no ambiguos.
- Bloquear la migracion si la deduplicacion de mensajes no puede garantizarse por tenant/proveedor/external_id.
- Resolver master y tenants desde la base master, no desde rutas arbitrarias de CLI.
- Crear y usar copias de trabajo con timestamp antes de aplicar cualquier upgrade real.
- Registrar el inventario y el plan operativo de cada base antes de tocar una base original.

## Documentacion

- Enumerar archivos modificados.
- Enumerar archivos creados.
- Registrar decisiones tecnicas.
- Registrar riesgos pendientes.

## Control de alcance

- Solo modificar archivos necesarios para el objetivo concreto.
- Documentar cualquier problema detectado fuera de alcance.

## Condicion de detencion

- Detenerse al terminar el alcance solicitado, sin empezar la fase siguiente.
