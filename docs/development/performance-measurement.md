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

## Salida del benchmark

El benchmark escribe dos ficheros por ejecucion:

- JSON con el detalle completo
- CSV con el resumen comparativo

La carpeta de salida es `backend/performance-results/` y esta excluida de Git.

## Fallos confirmados durante la medicion

- `/channels` ya no devuelve `500`; se corrigio la comparacion entre datetimes naive y aware.
- `/history` ya no devuelve `500`; se corrigio la referencia a `get_or_create_settings`.

Estos fallos quedaron cerrados en la fase 6A y el benchmark se rerun con exito para reflejar el estado actual.
