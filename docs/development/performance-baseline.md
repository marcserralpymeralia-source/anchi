# Linea base de rendimiento

## Estado actual

Esta es la referencia activa tras la Fase 6B. Home y workbench ya no crecen en consultas por cada elemento mostrado y se quedan en 11 consultas SQL con 1 consulta duplicada en los tres escenarios sinteticos.

Benchmark files actuales:

- `backend/performance-results/performance-baseline-small-20260715T094536Z.json`
- `backend/performance-results/performance-baseline-medium-20260715T094539Z.json`
- `backend/performance-results/performance-baseline-large-20260715T094546Z.json`

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

## Lectura rapida

- Home y workbench ya no cargan relaciones completas para el render inicial.
- Las lineas de pedido se resuelven con metadatos agregados.
- Las sugerencias de cliente en workbench se calculan una vez por request.
- El resto de rutas criticadas queda estable y sin regresion visible.
- `orders` y `history` siguen siendo las rutas mas pesadas fuera del alcance de esta fase.
