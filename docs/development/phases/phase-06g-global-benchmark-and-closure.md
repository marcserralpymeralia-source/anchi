# Fase 6G - Benchmark global y cierre del bloque de rendimiento

## Objetivo

Cerrar formalmente el bloque de rendimiento con una medicion global de todas las rutas criticas, compararla contra la linea base inicial y dejar claro que no se aplican mas optimizaciones en esta fase.

## Alcance ejecutado

- Verificacion inicial de Git.
- Validacion de la Fase 6F.
- Suite completa de tests.
- Suite especifica de rendimiento.
- Benchmark global en small, medium y large.
- Comparacion contra la base inicial del commit `01a2b26`.
- Revision de presupuestos.
- Documentacion final del bloque.

## Lo que se ha confirmado

- `/` se mantiene en 11 consultas y 1 duplicada.
- `/workbench` se mantiene en 11 consultas y 1 duplicada.
- `/channels` ya es una pagina medible y estable.
- `/orders` y `/orders/{id}` han bajado el peso estructural.
- `/history` ya no arrastra el historial completo.
- `/jobs/monitor`, `/settings`, `/customers`, `/products`, `/logs` y `/admin/diagnostics` siguen dentro de la ruta operativa normal.

## Validaciones ejecutadas

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_performance_instrumentation.PerformanceInstrumentationTests.test_history_page_loads_with_scoring_settings_present`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_home_workbench_optimization`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_orders_list_optimization`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_orders_detail_optimization`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_channels_optimization`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=development ./.venv/bin/python -m compileall app`
- `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"`
- `git diff --check`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large`

## Resultado global

| Ruta | Baseline inicial | Estado final | Lectura |
| --- | --- | --- | --- |
| `/` | 6.08 ms, 202 q, 191 dup | 2.96 ms, 11 q, 1 dup | Mejora estructural clara y estable |
| `/orders?date_range=90d` | 230.76 ms, 42 q, 26 dup | 6.22 ms, 14 q, 1 dup | Mejora muy fuerte |
| `/orders/1` | 7.19 ms, 25 q, 2 dup | 16.53 ms, 14 q, 0 dup | Menos queries; tiempo aun sensible al render y al volumen sintetico |
| `/channels?tab=processed&date_range=30d` | Ruta rota en baseline inicial | 8.78 ms, 10 q, 0 dup | Recuperada y estable |
| `/history?date_range=90d` | Ruta rota en fases previas | 8.29 ms, 19 q, 3 dup | Recuperada y ya paginada |

## Riesgos y trabajo futuro

- SQLite sigue influyendo mucho en los tiempos absolutos, asi que la validacion fina debe repetirse en PostgreSQL.
- `settings`, `jobs/monitor`, `customers`, `products` y `logs` siguen siendo las rutas que mas crecen con los escenarios grandes.
- El warning de `TemplateResponse` y los `ResourceWarning` de conexiones abiertas siguen siendo deuda tecnica, no regresiones funcionales.

## Estado final

- Bloque de rendimiento: cerrado
- Commit documental: pendiente de crear
