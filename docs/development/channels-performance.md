# Channels performance

## Objetivo

Dejar documentada la optimizacion de `/channels` realizada en la Fase 6E para que la bandeja quede basada en una pagina visible real, con resumen agregado y sin carga repetida por fila.

## Arquitectura de carga

La vista usa este flujo:

1. resolucion de tenant y settings;
2. union SQL de mensajes de correo e inbound;
3. filtros de pestaña, fecha, cliente, busqueda y score aplicados en SQL;
4. resumen agregado de pestañas y estados;
5. pagina visible con `LIMIT` y `OFFSET`;
6. adjuntos cargados solo para la pagina visible;
7. render Jinja con contexto ligero.

## Consultas principales

- union de emails e inbound por compania;
- agregado de contadores de pestañas y estados;
- pagina visible ordenada por fecha y `source_id`;
- adjuntos de emails y mensajes visibles;
- lista ligera de clientes para el filtro.

## Paginacion

- La paginacion se resuelve en SQL.
- No se filtra en memoria despues de cargar todo.
- El total y el rango visible salen del resumen y de la pagina actual.

## Contadores visibles

- `Todos`
- `Email`
- `WhatsApp`
- `Voz`
- `Redes`
- `Pendientes`
- `Procesados`
- `Revisión`
- `Errores`

## Reglas para evitar regresiones

- No volver a cargar todos los mensajes para pintar una sola pagina.
- No acceder a adjuntos completos desde Jinja.
- No ejecutar sugerencias o matching por fila durante el render.
- Mantener el aislamiento por compania.

## Metricas despues de la Fase 6E

| Escenario | Endpoint | Tiempo mediano | Queries | Duplicadas | Bytes | Registros cargados | Items mostrados |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Small | `/channels?tab=processed&date_range=30d` | 3.23 ms | 10 | 0 | 125802 | 20 | 20 |
| Medium | `/channels?tab=processed&date_range=30d` | 8.82 ms | 10 | 0 | 175472 | 25 | 25 |
| Large | `/channels?tab=processed&date_range=30d` | 28.22 ms | 10 | 0 | 199708 | 25 | 25 |

## Lectura rapida

- Se elimino la lectura ORM por fila.
- Los contadores se calculan una vez.
- El render mantiene la misma informacion visible.
- El coste se concentra ahora en una consulta principal paginada y en el contexto de filtros.
