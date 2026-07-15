# Linea base de rendimiento

## Fecha

2026-07-15

## Resumen ejecutivo

La app ya dispone de una linea base objetiva de rendimiento con tres escenarios sinteticos. La carga mas costosa esta concentrada en la bandeja principal y en el detalle de pedidos cuando crece el volumen de datos.

## Ranking de rendimiento

### Escenario small

| Posicion | Endpoint | Tiempo mediano | SQL | Duplicadas | Estado |
|---|---|---:|---:|---:|---:|
| 1 | `/channels?tab=processed&date_range=30d` | 17.64 ms | 0 | 0 | 500 |
| 2 | `/orders?date_range=90d` | 17.51 ms | 37 | 21 | 200 |
| 3 | `/products` | 4.84 ms | 9 | 0 | 200 |
| 4 | `/settings` | 4.52 ms | 68 | 0 | 200 |
| 5 | `/history?date_range=90d` | 4.19 ms | 0 | 0 | 500 |

### Escenario medium

| Posicion | Endpoint | Tiempo mediano | SQL | Duplicadas | Estado |
|---|---|---:|---:|---:|---:|
| 1 | `/orders?date_range=90d` | 230.76 ms | 42 | 26 | 200 |
| 2 | `/channels?tab=processed&date_range=30d` | 70.18 ms | 0 | 0 | 500 |
| 3 | `/jobs/monitor` | 8.82 ms | 14 | 5 | 200 |
| 4 | `/orders/1` | 7.19 ms | 25 | 2 | 200 |
| 5 | `/` | 6.08 ms | 202 | 191 | 200 |

### Escenario large

| Posicion | Endpoint | Tiempo mediano | SQL | Duplicadas | Estado |
|---|---|---:|---:|---:|---:|
| 1 | `/orders?date_range=90d` | 480.21 ms | 42 | 26 | 200 |
| 2 | `/channels?tab=processed&date_range=30d` | 133.40 ms | 0 | 0 | 500 |
| 3 | `/orders/1` | 15.21 ms | 26 | 2 | 200 |
| 4 | `/` | 11.90 ms | 410 | 399 | 200 |
| 5 | `/jobs/monitor` | 9.11 ms | 14 | 5 | 200 |

## Cuellos de botella confirmados

- La lista de pedidos crece con el volumen y eleva mucho el tiempo total y el render de plantilla.
- La home mantiene un numero alto de consultas y consultas duplicadas, sobre todo en escenarios medio y alto.
- El detalle de pedido carga bastante contexto y se nota mas en `large`.
- `channels` no puede formar parte de una medicion sana hasta corregir el bug de datetimes.

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

## Objetivos para la Fase 6

- Reducir peso de la home y de la bandeja.
- Limitar el coste del detalle de pedido.
- Encapsular mejor partes del render en bloques reutilizables.
- Corregir los errores confirmados de `channels` y `history`.
- Mantener el benchmark para comparar la evolucion futura.

