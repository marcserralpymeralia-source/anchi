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
- No filtrar secretos en logs estructurados ni en trazas persistidas.
- Documentar cualquier fallo que no se corrija en ese momento.

## Compatibilidad

- No romper el arranque local.
- No cambiar rutas ni endpoints salvo que el objetivo lo requiera.
- No alterar el modelo de datos salvo instruccion explicita.
- No permitir acceso cruzado entre dos companias con IDs coincidentes en bases distintas.
- Mantener jobs idempotentes, con reintentos limitados, historial de intentos y recovery de jobs bloqueados.
- Garantizar que el worker tenga una entrada CLI estable y que no procese un job antes de su fecha de reintento.
- No activar profiling en produccion.

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
