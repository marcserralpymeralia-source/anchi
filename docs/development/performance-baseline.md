# Linea base de rendimiento

## Fecha

2026-07-15

## Resumen ejecutivo

La app ya dispone de una linea base objetiva de rendimiento con tres escenarios sinteticos. La carga mas costosa sigue concentrada en la bandeja principal y en el detalle de pedidos cuando crece el volumen de datos, pero los fallos de `channels` y `history` ya quedaron corregidos y vuelven a entrar en la medicion normal.

## Ranking de rendimiento

### Escenario small

| Posicion | Endpoint | Tiempo mediano | SQL | Duplicadas | Estado |
|---|---|---:|---:|---:|---:|
| 1 | `/orders?date_range=90d` | 17.43 ms | 37 | 21 | 200 |
| 2 | `/history?date_range=90d` | 4.45 ms | 33 | 0 | 200 |
| 3 | `/products` | 4.31 ms | 9 | 0 | 200 |
| 4 | `/` | 4.08 ms | 50 | 39 | 200 |
| 5 | `/settings` | 3.83 ms | 68 | 0 | 200 |

### Escenario medium

| Posicion | Endpoint | Tiempo mediano | SQL | Duplicadas | Estado |
|---|---|---:|---:|---:|---:|
| 1 | `/orders?date_range=90d` | 232.67 ms | 42 | 26 | 200 |
| 2 | `/history?date_range=90d` | 12.20 ms | 153 | 141 | 200 |
| 3 | `/jobs/monitor` | 8.71 ms | 14 | 5 | 200 |
| 4 | `/channels?tab=processed&date_range=30d` | 6.51 ms | 132 | 119 | 200 |
| 5 | `/orders/1` | 6.49 ms | 25 | 2 | 200 |

### Escenario large

| Posicion | Endpoint | Tiempo mediano | SQL | Duplicadas | Estado |
|---|---|---:|---:|---:|---:|
| 1 | `/orders?date_range=90d` | 496.89 ms | 42 | 26 | 200 |
| 2 | `/history?date_range=90d` | 18.58 ms | 313 | 301 | 200 |
| 3 | `/orders/1` | 16.13 ms | 26 | 2 | 200 |
| 4 | `/` | 12.06 ms | 410 | 399 | 200 |
| 5 | `/channels?tab=processed&date_range=30d` | 9.91 ms | 245 | 232 | 200 |

## Cuellos de botella confirmados

- La lista de pedidos crece con el volumen y eleva mucho el tiempo total y el render de plantilla.
- La home mantiene un numero alto de consultas y consultas duplicadas, sobre todo en escenarios medio y alto.
- El detalle de pedido carga bastante contexto y se nota mas en `large`.
- `channels` y `history` ya no fallan; ahora quedan dentro de la linea base como rutas normales.

## N+1 confirmados

- En la home se observan muchas consultas repetidas en todos los escenarios, con 39, 191 y 399 duplicadas segun el volumen.
- En el detalle de pedido se repite carga de contexto y parte del coste proviene de consultas equivalentes.

## Paginacion y carga en memoria

- `orders` y `orders/{id}` cargan volumen elevado de registros en memoria para construir la vista.
- En `large`, `orders` carga 1385 registros y el detalle 1360, con una respuesta muy pesada.
- `customers`, `products`, `logs` y `admin/diagnostics` se mantienen mas acotados, pero el crecimiento del volumen sigue siendo visible.

## Polling y llamadas sincronas

- No se han movido trabajos pesados a jobs en esta fase.
- El benchmark no altera la frecuencia de polling ni la estrategia de sincronizacion.
- Las rutas de medicion siguen siendo llamadas sincronas para obtener una foto real del coste actual.

## Objetivos para la Fase 6A

- Corregir los fallos de `channels` y `history`.
- Mantener el benchmark como referencia valida.
- Seguir reduciendo peso de home y bandeja en fases posteriores.
- Conservar la linea base sintetica para comparar futuras mejoras.
