# Medicion objetiva de rendimiento

## Objetivo

Definir una linea base repetible para medir el coste real de las rutas criticas de la app sin aplicar optimizaciones funcionales ni cambios de comportamiento.

## Que se mide

La instrumentacion de rendimiento recoge, por request:

- duracion total
- duracion SQL
- numero total de consultas
- consultas duplicadas por sentencia normalizada
- duracion de renderizado de templates Jinja
- tamano de respuesta
- registros cargados
- items mostrados en pantalla

## Mecanismo tecnico

- `backend/app/core/performance.py` agrega el collector de rendimiento.
- `backend/app/core/templating.py` mide el render de templates y acumula tiempo de plantilla.
- `backend/app/core/middleware.py` expone cabeceras `X-Perf-*` y registra la traza.
- `backend/scripts/measure_performance.py` ejecuta la bateria reproducible y guarda JSON/CSV.
- `backend/scripts/performance_data.py` crea escenarios sinteticos temporales en master + tenant aislados.

## Escenarios de volumen

- `small`
- `medium`
- `large`

Los tres escenarios usan datos sinteticos y temporales. No se uso informacion real.

## Rutas criticadas

- `/`
- `/workbench`
- `/channels?tab=processed&date_range=30d`
- `/orders?date_range=90d`
- `/orders/{order_id}`
- `/history?date_range=90d`
- `/customers`
- `/products`
- `/imports/quick`
- `/settings`
- `/jobs/monitor`
- `/alerts`
- `/logs`
- `/admin/diagnostics`

La fase 6C se concentro solo en el listado general de pedidos (`/orders`) y dejo intacto el detalle (`/orders/{order_id}`), que sigue siendo una pantalla de trabajo completa.

## Salida del benchmark

El benchmark escribe dos ficheros por ejecucion:

- JSON con el detalle completo
- CSV con el resumen comparativo

La carpeta de salida es `backend/performance-results/` y esta excluida de Git.

## Fallos confirmados durante la medicion

- `/channels` ya no devuelve `500`; se corrigio la comparacion entre datetimes naive y aware.
- `/history` ya no devuelve `500`; se corrigio la referencia a `get_or_create_settings`.

Estos fallos quedaron cerrados en la fase 6A y el benchmark se rerun con exito para reflejar el estado actual.

## Nota de la fase 6B

En la fase 6B, home y workbench dejan de depender de carga lazy por fila para su render inicial. La vista usa metadatos agregados de lineas y un mapa compartido de sugerencias de cliente para reducir consultas repetidas sin cambiar la experiencia visible ni el comportamiento de la pantalla.

## Nota de la fase 6C

El listado general de pedidos ya no incrusta el detalle completo por fila. Ahora carga una pagina SQL real, trae solo las relaciones necesarias para la tabla visible y calcula alertas con agregados por pagina. El resultado es una reduccion fuerte de HTML, consultas y memoria, manteniendo los filtros operativos y sin tocar el detalle completo del pedido.

## Nota de la fase 6D

El detalle de pedido deja de comportarse como una pantalla de carga amplia para cliente y producto. La medicion de la fase 6D se centra en un render estable del pedido con snapshot de cabecera, candidatos compartidos y sin recuperar catalogos completos por relaciones lazy. El benchmark refleja ese cambio porque ya no arrastra cientos de registros en memoria para pintar una sola ficha operativa.

## Nota de la fase 6E

La bandeja `/channels` pasa a medir una pagina visible real y un resumen agregado, sin ejecutar consultas por fila ni cargar colecciones completas para flags o estados visibles. El render inicial mantiene la misma experiencia operativa, pero ahora la carga se concentra en una consulta principal paginada, un agregado de contadores y un fetch acotado de adjuntos para las filas visibles.

## Nota de la fase 6F

La pantalla `/history` deja de materializar el historial completo en memoria y pasa a resolver filtros, ordenacion, resumen y paginacion desde SQL. La vista sigue mostrando correos y pedidos mezclados en la misma experiencia operativa, pero ahora la carga se centra en la pagina visible y en un conjunto acotado de filas ORM.
