# Linea base de rendimiento

## Estado actual

Esta es la referencia activa tras la Fase 6F. Home y workbench ya no crecen en consultas por cada elemento mostrado, el listado general de pedidos quedo reducido en la Fase 6C, el detalle de pedido se optimizo en la Fase 6D, la bandeja `/channels` paso a una carga SQL real en la Fase 6E y `/history` dejo de cargar la coleccion completa para trabajar con paginacion y agregados SQL.

Benchmark files actuales:

- `backend/performance-results/performance-baseline-small-20260715T135956Z.json`
- `backend/performance-results/performance-baseline-medium-20260715T135957Z.json`
- `backend/performance-results/performance-baseline-large-20260715T140008Z.json`
- `backend/performance-results/performance-baseline-small-20260715T142118Z.json`
- `backend/performance-results/performance-baseline-medium-20260715T142119Z.json`
- `backend/performance-results/performance-baseline-large-20260715T142120Z.json`
- `backend/performance-results/performance-baseline-small-20260715T113120Z.json`
- `backend/performance-results/performance-baseline-medium-20260715T113344Z.json`
- `backend/performance-results/performance-baseline-large-20260715T113429Z.json`

## Channels: antes y despues

La bandeja `/channels?tab=processed&date_range=30d` dejaba de ser util porque mezclaba la carga visible con consultas repetidas y contexto pesado por fila. La fase 6E la convierte en una pagina SQL con paginacion real, resumen agregado y adjuntos cargados solo para las filas visibles.

| Escenario | Antes | Despues | Mejora |
| --- | --- | --- | --- |
| Small | 6.30 ms, 132 queries, 119 duplicadas, 175512 bytes, 334 registros cargados | 3.23 ms, 10 queries, 0 duplicadas, 125802 bytes, 20 registros cargados | -48.7% tiempo, -92.4% queries, -100.0% duplicadas, -28.4% bytes, -94.0% registros |
| Medium | 6.30 ms, 132 queries, 119 duplicadas, 175512 bytes, 334 registros cargados | 8.82 ms, 10 queries, 0 duplicadas, 175472 bytes, 25 registros cargados | -92.4% queries, -100.0% duplicadas, -92.5% registros |
| Large | 6.30 ms, 132 queries, 119 duplicadas, 175512 bytes, 334 registros cargados | 28.22 ms, 10 queries, 0 duplicadas, 199708 bytes, 25 registros cargados | -92.4% queries, -100.0% duplicadas, -92.5% registros |

## Channels: resumen tecnico

- Consulta principal en SQL con `WHERE`, `ORDER BY`, `LIMIT` y `OFFSET`.
- Resumen de pestañas y contadores por canal calculados en una consulta agregada.
- Adjunto y PDF cargados solo para las filas visibles.
- Contexto de filtros y clientes mantenido con estructuras ligeras para no inflar el render.
- Aislamiento tenant mantenido sin cambiar la experiencia visible de la bandeja.

## History: antes y despues

La fase 6F convierte `/history` en una pagina SQL paginada, con filtros, resumen y ordenacion resueltos en base de datos. El objetivo era eliminar la carga completa de historial y limitar el trabajo a la pagina visible.

| Escenario | Antes | Despues |
| --- | --- | --- |
| Small | 4.39 ms, 33 queries, 21 duplicadas, 39023 bytes, 60 registros cargados | 18.74 ms, 19 queries, 3 duplicadas, 39023 bytes, 60 registros cargados |
| Medium | 6.29 ms, 153 queries, 141 duplicadas, 78848 bytes, 350 registros cargados | 27.05 ms, 19 queries, 3 duplicadas, 56040 bytes, 210 registros cargados |
| Large | 25.01 ms, 313 queries, 301 duplicadas, 98378 bytes, 730 registros cargados | 29.92 ms, 19 queries, 3 duplicadas, 75487 bytes, 430 registros cargados |

Nota: el mayor avance de esta fase esta en la reduccion de consultas y duplicadas. El tiempo medido sigue dependiendo bastante del volumen sintetico y del coste de render de la pagina visible.

## History: resumen tecnico

- Consulta unificada de pedidos y correos con filtros SQL.
- Paginacion, orden y conteos por pestaña resueltos en base de datos.
- Carga ORM solo para los elementos visibles de la pagina actual.
- Reutilizacion de mapas de sugerencia de cliente y metadatos de lineas.
- Menos trabajo en Python antes del render Jinja.

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

## Fase 6G - benchmark global

La Fase 6G no introduce optimizacion nueva. Su cierre confirma el estado final del bloque de rendimiento con el benchmark completo y compara el resultado con la linea base inicial del commit `01a2b26`.

### Consolidado final de rutas criticas

| Ruta | Small | Medium | Large |
| --- | --- | --- | --- |
| `/` | 2.24 ms, 11 q, 1 dup, 95 KB, 20 registros | 2.96 ms, 11 q, 1 dup, 114 KB, 25 registros | 3.09 ms, 11 q, 1 dup, 114 KB, 25 registros |
| `/workbench` | 0.37 ms, 11 q, 1 dup, 16 KB, 0 registros | 0.67 ms, 11 q, 1 dup, 21 KB, 0 registros | 1.12 ms, 11 q, 1 dup, 21 KB, 0 registros |
| `/channels?tab=processed&date_range=30d` | 3.28 ms, 10 q, 0 dup, 125 KB, 20 registros | 8.78 ms, 10 q, 0 dup, 175 KB, 25 registros | 51.60 ms, 10 q, 0 dup, 199 KB, 25 registros |
| `/orders?date_range=90d` | 4.75 ms, 14 q, 1 dup, 56 KB, 20 registros | 6.22 ms, 14 q, 1 dup, 78 KB, 25 registros | 18.86 ms, 14 q, 1 dup, 97 KB, 25 registros |
| `/orders/1` | 1.88 ms, 14 q, 0 dup, 30 KB, 3 registros | 16.53 ms, 14 q, 0 dup, 30 KB, 3 registros | 117.96 ms, 14 q, 0 dup, 30 KB, 3 registros |
| `/history?date_range=90d` | 4.86 ms, 19 q, 3 dup, 39 KB, 60 registros | 8.29 ms, 19 q, 3 dup, 56 KB, 210 registros | 22.45 ms, 19 q, 3 dup, 75 KB, 430 registros |
| `/jobs/monitor` | 0.79 ms, 14 q, 5 dup, 17 KB, 0 registros | 23.24 ms, 14 q, 5 dup, 79 KB, 25 registros | 15.81 ms, 14 q, 5 dup, 79 KB, 25 registros |
| `/customers` | 2.42 ms, 32 q, 4 dup, 94 KB, 40 registros | 3.60 ms, 32 q, 0 dup, 112 KB, 50 registros | 8.60 ms, 32 q, 0 dup, 112 KB, 50 registros |
| `/products` | 4.45 ms, 9 q, 0 dup, 114 KB, 20 registros | 6.29 ms, 9 q, 0 dup, 138 KB, 25 registros | 10.85 ms, 9 q, 0 dup, 138 KB, 25 registros |
| `/imports/quick` | 0.53 ms, 15 q, 4 dup, 16 KB, 0 registros | 0.64 ms, 15 q, 4 dup, 16 KB, 0 registros | 1.46 ms, 15 q, 4 dup, 16 KB, 0 registros |
| `/settings` | 4.62 ms, 68 q, 25 dup, 73 KB, 22 registros | 15.13 ms, 68 q, 25 dup, 73 KB, 22 registros | 12.57 ms, 68 q, 25 dup, 73 KB, 22 registros |
| `/alerts` | 0.71 ms, 8 q, 0 dup, 28 KB, 4 registros | 6.60 ms, 8 q, 0 dup, 89 KB, 4 registros | 3.07 ms, 8 q, 0 dup, 89 KB, 4 registros |
| `/logs` | 0.37 ms, 5 q, 0 dup, 15 KB, 1 registro | 3.65 ms, 5 q, 0 dup, 56 KB, 91 registros | 5.03 ms, 5 q, 0 dup, 97 KB, 181 registros |
| `/admin/diagnostics` | 0.69 ms, 22 q, 5 dup, 18 KB, 1 registro | 1.95 ms, 22 q, 5 dup, 18 KB, 1 registro | 1.74 ms, 22 q, 5 dup, 18 KB, 1 registro |

### Comparacion con la base inicial

| Ruta | Baseline inicial 01a2b26 | Estado final | Lectura |
| --- | --- | --- | --- |
| `/` | 6.08 ms, 202 q, 191 dup | 2.96 ms, 11 q, 1 dup | Caida estructural fuerte y estable |
| `/orders?date_range=90d` | 230.76 ms, 42 q, 26 dup | 6.22 ms, 14 q, 1 dup | Reduccion muy grande de coste y de ruido ORM |
| `/orders/1` | 7.19 ms, 25 q, 2 dup | 16.53 ms, 14 q, 0 dup | Queries mejoran; tiempo sigue sensible al render y al volumen sintetico |
| `/channels?tab=processed&date_range=30d` | 70.18 ms y 500 en small por la regresion, sin baseline util estable | 8.78 ms, 10 q, 0 dup | La ruta queda recuperada y ya es medible con normalidad |
| `/history?date_range=90d` | 4.19 ms en small, pero la ruta estaba rota en escenarios posteriores | 8.29 ms, 19 q, 3 dup | Ya no hay 500 y la pagina trabaja sobre SQL paginado |

### Conclusiones de cierre

- La mejora estructural mas clara sigue siendo la reduccion de consultas y duplicadas.
- Los tiempos siguen dependiendo de SQLite, del volumen sintetico y del render de plantilla en algunas rutas.
- `jobs/monitor`, `settings`, `customers`, `products` y `logs` son ahora los principales candidatos para repetir medicion en PostgreSQL antes de sacar conclusiones de latencia fina.
- No se han introducido regresiones funcionales en la suite completa.
- El bloque de rendimiento puede darse por cerrado a nivel documental.
