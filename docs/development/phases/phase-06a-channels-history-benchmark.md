# Fase 6A - Restaurar `channels` e `history` y completar el benchmark

## 1. Objetivo

Cerrar dos regresiones funcionales que rompian la medicion y la operativa basica:

- `/channels` devolvia `500` por comparaciones entre datetimes naive y aware.
- `/history` devolvia `500` por una referencia no importada a `get_or_create_settings`.

Ademas, regenerar la base de rendimiento para que el benchmark vuelva a reflejar el estado real de la app.

## 2. Alcance ejecutado

- Normalizacion de datetimes al construir y filtrar entradas en `channels`.
- Import correcto de `get_or_create_settings` en `history`.
- Cobertura de regresion para ambos endpoints.
- Reejecucion del benchmark sintetico en `small`, `medium` y `large`.
- Actualizacion de la documentacion de rendimiento y del registro de fase.

## 3. Alcance no ejecutado

- No se optimizaron consultas ni templates.
- No se añadieron indices.
- No se modifico la paginacion.
- No se introdujo caching.
- No se movieron jobs.
- No se cambio la interfaz.
- No se crearon migraciones.
- No se añadieron dependencias.
- No se uso dato real.

## 4. Cambios principales

| Archivo | Cambio |
|---|---|
| `backend/app/channels/routes.py` | Se normalizaron datetimes a UTC antes de filtrar los resultados |
| `backend/app/pages/routes.py` | Se importo `get_or_create_settings` y se normalizaron fechas del historial |
| `backend/tests/test_performance_instrumentation.py` | Se añadieron regresiones para `/channels` y `/history` |
| `docs/development/performance-measurement.md` | Se actualizaron los fallos confirmados tras la correccion |
| `docs/development/performance-baseline.md` | Se refresco la linea base con los resultados nuevos |
| `docs/development/change-log.md` | Se registro el cierre de la fase 6A |
| `docs/development/decision-log.md` | Se registro la decision de cerrar las dos regresiones antes de seguir |

## 5. Validaciones

- `./.venv/bin/python -m compileall app`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_performance_instrumentation`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large`

## 6. Riesgos y observaciones

- La homogeneidad naive/aware puede reaparecer si otras rutas vuelven a consumir datetimes de SQLite sin normalizar.
- El benchmark sigue mostrando que la home y `orders` son los puntos mas costosos.
- La correccion de esta fase no cambia la estrategia de carga ni el peso de templates.
