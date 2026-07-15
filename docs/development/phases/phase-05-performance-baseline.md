# Fase 5 - Medicion objetiva de rendimiento

## 1. Objetivo

Construir una linea base objetiva y reproducible de rendimiento para las rutas criticas de la aplicacion, sin introducir optimizaciones, indices, paginacion nueva, caching, migraciones ni cambios de interfaz.

## 2. Alcance ejecutado

- Instrumentacion de duracion total, SQL, consultas duplicadas, render de templates, tamano de respuesta y volumen de registros.
- Script reproducible de benchmark con tres escenarios sinteticos.
- Generacion de resultados JSON y CSV en `backend/performance-results/`.
- Tests de la instrumentacion y del benchmark.
- Documentacion de medicion, baseline, decisiones y cambios.

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
| `backend/app/core/performance.py` | Collector de rendimiento, cabeceras `X-Perf-*` y medicion SQL/template |
| `backend/app/core/templating.py` | Templates conscientes de la instrumentacion de rendimiento |
| `backend/app/core/app_factory.py` | Arranque de la capa de rendimiento |
| `backend/app/core/config.py` | Nueva configuracion segura para activar profiling |
| `backend/scripts/performance_data.py` | Fixtures sinteticas por escenario para master + tenant aislados |
| `backend/scripts/measure_performance.py` | Benchmark reproducible con JSON/CSV |
| `backend/tests/test_performance_instrumentation.py` | Cobertura de la instrumentacion y del script |
| `backend/.env.example` | Variable de entorno para activar profiling |
| `.gitignore` | Exclusion de `backend/performance-results/` |
| `docs/development/*` | Guia, comandos, criterios y registro documental de la fase |

## 5. Validaciones

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_performance_instrumentation`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `./.venv/bin/python -m compileall app`
- `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large`

## 6. Riesgos y observaciones

- `/channels` sigue devolviendo `500` por un bug de datetimes.
- `/history` sigue devolviendo `500` por una referencia no definida.
- Existen consultas repetidas visibles en la linea base; no se han atacado en esta fase.
- El baseline depende de datos sinteticos temporales y debe regenerarse si cambia la estructura de datos o los endpoints medidos.

