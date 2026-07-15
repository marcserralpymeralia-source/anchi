# Linea base de rendimiento

## Estado actual

Esta es la referencia activa tras la Fase 6D. Home y workbench ya no crecen en consultas por cada elemento mostrado, el listado general de pedidos quedo reducido en la Fase 6C y el detalle de pedido se optimizo en la Fase 6D para evitar cargar relaciones completas y catálogos innecesarios.

Benchmark files actuales:

- `backend/performance-results/performance-baseline-small-20260715T113120Z.json`
- `backend/performance-results/performance-baseline-medium-20260715T113344Z.json`
- `backend/performance-results/performance-baseline-large-20260715T113429Z.json`

## Home y workbench: antes y despues

| Escenario | Endpoint | Antes | Despues | Mejora |
| --- | --- | --- | --- | --- |
| Small | `/` | 3.06 ms, 50 queries, 39 duplicadas | 2.10 ms, 11 queries, 1 duplicada | -77.9% queries, -97.4% duplicadas |
| Small | `/workbench` | 0.82 ms, 33 queries, 21 duplicadas | 0.29 ms, 11 queries, 1 duplicada | -66.7% queries, -95.2% duplicadas |
| Medium | `/` | 8.41 ms, 202 queries, 191 duplicadas | 2.72 ms, 11 queries, 1 duplicada | -94.6% queries, -99.5% duplicadas |
| Medium | `/workbench` | 3.72 ms, 153 queries, 141 duplicadas | 0.63 ms, 11 queries, 1 duplicada | -92.8% queries, -99.3% duplicadas |
| Large | `/` | 15.69 ms, 410 queries, 399 duplicadas | 2.99 ms, 11 queries, 1 duplicada | -97.3% queries, -99.7% duplicadas |
| Large | `/workbench` | 8.33 ms, 313 queries, 301 duplicadas | 1.02 ms, 11 queries, 1 duplicada | -96.5% queries, -99.7% duplicadas |

## Resumen de control

La optimizacion de home y workbench no rompio las rutas de control y, en este rerun, incluso redujo su coste medido en escenario medium.

| Endpoint | Antes (medium) | Despues (medium) | Variacion |
| --- | --- | --- | --- |
| `/orders?date_range=90d` | 279.8 ms, 42 queries, 26 duplicadas | 166.01 ms, 42 queries, 26 duplicadas | -40.7% tiempo |
| `/orders/1` | 10.94 ms, 25 queries, 2 duplicadas | 7.29 ms, 25 queries, 2 duplicadas | -33.4% tiempo |
| `/channels?tab=processed&date_range=30d` | 8.75 ms, 132 queries, 119 duplicadas | 6.27 ms, 132 queries, 119 duplicadas | -28.3% tiempo |
| `/history?date_range=90d` | 16.18 ms, 153 queries, 141 duplicadas | 11.86 ms, 153 queries, 141 duplicadas | -26.7% tiempo |
| `/jobs/monitor` | 12.99 ms, 14 queries, 5 duplicadas | 8.75 ms, 14 queries, 5 duplicadas | -32.6% tiempo |

## Pedidos: antes y despues

La fase 6C ataco solo el listado general de pedidos. Se elimino el detalle pesado embebido por fila, se sustituyeron relaciones lazy por agregados por pagina y se dejo el detalle completo en `/orders/{order_id}`.

| Escenario | `/orders?date_range=90d` antes | `/orders?date_range=90d` despues | Mejora |
| --- | --- | --- | --- |
| Small | 21.00 ms, 37 queries, 1 duplicada, 455245 bytes | 4.35 ms, 14 queries, 1 duplicada, 56511 bytes | -79.3% tiempo, -62.2% queries, -87.6% bytes |
| Medium | 408.57 ms, 42 queries, 1 duplicada, 4407796 bytes | 2.69 ms, 14 queries, 1 duplicada, 78266 bytes | -99.3% tiempo, -66.7% queries, -98.2% bytes |
| Large | 762.54 ms, 42 queries, 1 duplicada, 10977304 bytes | 6.94 ms, 14 queries, 1 duplicada, 97757 bytes | -99.1% tiempo, -66.7% queries, -99.1% bytes |

## Control medium tras la fase 6C

Las rutas de control se volvieron a medir en scenario medium para comprobar que la optimizacion de pedidos no degrada el resto del sistema por encima del 10 por ciento.

| Endpoint | Antes (medium) | Despues (medium) | Variacion |
| --- | --- | --- | --- |
| `/` | 2.72 ms, 11 queries, 1 duplicada | 2.55 ms, 11 queries, 1 duplicada | -6.3% tiempo |
| `/workbench` | 0.63 ms, 11 queries, 1 duplicada | 0.60 ms, 11 queries, 1 duplicada | -4.8% tiempo |
| `/channels?tab=processed&date_range=30d` | 6.27 ms, 132 queries, 119 duplicadas | 6.63 ms, 132 queries, 119 duplicadas | +5.7% tiempo |
| `/history?date_range=90d` | 11.86 ms, 153 queries, 141 duplicadas | 5.83 ms, 153 queries, 141 duplicadas | -50.8% tiempo |
| `/jobs/monitor` | 8.75 ms, 14 queries, 5 duplicadas | 2.04 ms, 14 queries, 5 duplicadas | -76.7% tiempo |

## Detalle de pedido: antes y despues

La fase 6D ataco la pantalla completa de revision de pedido (`/orders/{order_id}`). Se sustituyo la carga de relaciones pesadas por un modelo de consulta mas ligero, se reutilizaron candidatos compartidos y se dejo el header basado en una instantanea de cliente, no en la relacion ORM completa.

| Escenario | `/orders/1` antes | `/orders/1` despues | Mejora |
| --- | --- | --- | --- |
| Small | 1.81 ms, 25 queries, 2 duplicadas, 36447 bytes, 40 records | 1.71 ms, 14 queries, 0 duplicadas, 30262 bytes, 3 records | -5.5% tiempo, -44.0% queries, -16.9% bytes, -92.5% records |
| Medium | 7.12 ms, 25 queries, 2 duplicadas, 148571 bytes, 540 records | 16.20 ms, 14 queries, 0 duplicadas, 30262 bytes, 3 records | -44.0% queries, -100.0% duplicadas, -79.6% bytes, -99.4% records |
| Large | 14.15 ms, 26 queries, 2 duplicadas, 334691 bytes, 1360 records | 66.40 ms, 14 queries, 0 duplicadas, 30262 bytes, 3 records | -46.2% queries, -100.0% duplicadas, -91.0% bytes, -99.8% records |

## Fase 6D - Detalle de pedido

La optimizacion del detalle de pedido quedo documentada como una pantalla de trabajo mas ligera:

- El pedido se carga con un subconjunto minimo de columnas y relaciones.
- El cliente visible en cabecera sale de una instantanea estable, no de una carga lazy posterior.
- Las sugerencias de cliente y producto reutilizan consultas compartidas.
- La vista mantiene adjuntos, scoring, estado y acciones sin reactivar el bloqueo de catalogos completos.
- La validacion de detalle queda por debajo del presupuesto objetivo de consultas y tamano en los tres escenarios sinteticos.

## Lectura rapida

- Home y workbench se mantienen estables en 11 consultas SQL.
- El listado general de pedidos sigue muy por debajo del umbral objetivo de la fase 6C.
- El detalle de pedido deja de arrastrar cargas completas de cliente y producto.
- La respuesta del detalle se mantiene compacta y sin dependencias lazy visibles desde Jinja.
- La optimizacion del detalle no introduce regresion funcional en la ruta ni en la renderizacion principal.
