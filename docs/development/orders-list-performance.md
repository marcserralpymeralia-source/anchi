# Rendimiento del listado general de pedidos

## Objetivo

Reducir de forma fuerte el coste del listado general de pedidos sin tocar la pantalla de detalle completa ni cambiar los filtros visibles.

## Diagnostico

Antes de la fase 6C, `/orders?date_range=90d` arrastraba un bloque de detalle enorme por cada fila:

- correo completo
- adjuntos y PDF embebidos
- lineas interpretadas
- formularios de cliente y producto
- vista previa de exportacion

Eso disparaba el HTML, las consultas y el tiempo de render aunque el usuario solo necesitara una vista resumen.

## Cambio aplicado

- Se mantuvo la paginacion SQL real.
- Se eliminaron las cargas lazy por fila que dependian de `order.lines` y `email.attachments`.
- Se limitaron las relaciones cargadas al resumen visible de la tabla.
- Se calcularon alertas por lote a partir de agregados por pagina.
- Se sustituyo el detalle embebido por enlace directo a `/orders/{order_id}`.

## Resultados actuales

| Escenario | Duracion | Queries SQL | Duplicadas | Tamaño HTML |
| --- | --- | ---: | ---: | ---: |
| Small | 4.35 ms | 14 | 1 | 56,511 bytes |
| Medium | 2.62 ms | 14 | 1 | 78,266 bytes |
| Large | 6.91 ms | 14 | 1 | 97,757 bytes |

## Comparacion con la linea anterior

| Escenario | Antes | Ahora | Mejora |
| --- | --- | --- | --- |
| Small | 21.00 ms, 37 queries, 455,245 bytes | 4.35 ms, 14 queries, 56,511 bytes | Mucho menos peso en render y datos |
| Medium | 408.57 ms, 42 queries, 4,407,796 bytes | 2.62 ms, 14 queries, 78,266 bytes | La lista deja de crecer con el contenido oculto |
| Large | 762.54 ms, 42 queries, 10,977,304 bytes | 6.91 ms, 14 queries, 97,757 bytes | Se elimina el coste lineal por fila |

## Control medium

Las rutas de control quedaron dentro del margen esperado de la fase:

- `/`
- `/workbench`
- `/channels?tab=processed&date_range=30d`
- `/history?date_range=90d`
- `/jobs/monitor`

La unica variacion visible fue en `channels`, con un aumento de 5.7% en tiempo, por debajo del umbral de 10%.

## Validacion

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_orders_list_optimization`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large`
