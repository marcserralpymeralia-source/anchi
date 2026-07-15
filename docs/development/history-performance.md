# History performance

## Objetivo

Dejar documentado como se carga `/history` tras la Fase 6F, con el historial resuelto como una pagina SQL visible y no como una coleccion completa materializada antes del render.

## Arquitectura de carga

La vista usa este flujo:

1. resolucion de tenant y settings;
2. union SQL de filas de pedidos y correos;
3. filtros de fecha, estado, tipo, cliente y busqueda aplicados en SQL;
4. conteos y resumen calculados en una consulta agregada;
5. pagina visible con `LIMIT` y `OFFSET`;
6. carga ORM solo de los pedidos y correos de esa pagina;
7. render Jinja con contexto ligero.

## Consultas principales

- union de pedidos y correos por compania;
- agregado de contadores por estado y categoria;
- pagina visible ordenada por fecha;
- carga acotada de pedidos visibles;
- carga acotada de correos visibles y sus adjuntos;
- mapas compartidos de sugerencias y metadatos para evitar consultas repetidas.

## Reglas para evitar regresiones

- No volver a cargar todo el historial para pintar una pagina.
- No ejecutar matching por fila durante el render si puede resolverse una sola vez.
- No acceder a relaciones lazy desde la plantilla.
- Mantener el aislamiento por compania.

## Metricas despues de la Fase 6F

| Escenario | Endpoint | Tiempo mediano | Queries | Duplicadas | Bytes | Registros cargados | Items mostrados |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Small | `/history?date_range=90d` | 18.74 ms | 19 | 3 | 39023 | 60 | 20 |
| Medium | `/history?date_range=90d` | 27.05 ms | 19 | 3 | 56040 | 210 | 25 |
| Large | `/history?date_range=90d` | 29.92 ms | 19 | 3 | 75487 | 430 | 25 |

Nota: la reduccion fuerte de esta fase esta en consultas y duplicadas. La latencia mediana sigue variando por escenario, pero la pagina visible ya no depende de cargar el historial completo.

## Lectura rapida

- El historial deja de cargar todo el universo antes del render.
- Los filtros y el orden se resuelven en SQL.
- La pagina visible se mantiene operativa y ligera.
- El coste cae a una sola pagina SQL mas el contexto minimo de render.
