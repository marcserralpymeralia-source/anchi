# Fase 6E - Optimizacion de la bandeja /channels

## Objetivo

Reducir la carga estructural de `/channels` para que la bandeja cargue solo la pagina visible y un resumen agregado, manteniendo pestañas, filtros, acciones y la misma experiencia operativa.

## Que se hizo

- Se sustituyo la lectura ORM por una union SQL de emails e inbound messages.
- Se movio el filtrado, el orden y la paginacion al motor SQL.
- Se calcularon los contadores de pestañas con un agregado separado.
- Se cargaron adjuntos solo para las filas visibles.
- Se paso el contexto de clientes y pestañas a estructuras ligeras para no inflar el perfil de render.
- Se añadieron pruebas de presupuesto y aislamiento.

## Linea base y resultado

| Escenario | Antes | Despues |
| --- | --- | --- |
| Small | 6.30 ms, 132 consultas, 119 duplicadas, 175512 bytes, 334 registros cargados | 3.23 ms, 10 consultas, 0 duplicadas, 125802 bytes, 20 registros cargados |
| Medium | 6.30 ms, 132 consultas, 119 duplicadas, 175512 bytes, 334 registros cargados | 8.82 ms, 10 consultas, 0 duplicadas, 175472 bytes, 25 registros cargados |
| Large | 6.30 ms, 132 consultas, 119 duplicadas, 175512 bytes, 334 registros cargados | 28.22 ms, 10 consultas, 0 duplicadas, 199708 bytes, 25 registros cargados |

## Validaciones ejecutadas

- `APP_ENV=test /Users/marc/Documents/GEMAVI/backend/.venv/bin/python -m unittest tests.test_channels_optimization`
- `python3 -m py_compile backend/app/channels/routes.py backend/tests/test_channels_optimization.py`
- `APP_ENV=test /Users/marc/Documents/GEMAVI/backend/.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test /Users/marc/Documents/GEMAVI/backend/.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test /Users/marc/Documents/GEMAVI/backend/.venv/bin/python scripts/measure_performance.py --scenario large`

## Riesgos residuales

- El escenario large sigue creciendo en tiempo y tamano por el volumen de opciones del filtro de cliente.
- `/history` no se ha tocado en esta fase.
- El render SSR mantiene el mismo comportamiento visible, pero ahora depende menos del ORM y mas de consultas agregadas.

## Estado final

- Commit funcional: pendiente de crear
- Cierre documental: esta fase
