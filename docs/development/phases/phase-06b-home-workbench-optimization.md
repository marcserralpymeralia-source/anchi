# Fase 6B - Optimizacion de home y workbench

## 1. Objetivo

Optimizar la carga inicial de home y workbench para eliminar consultas repetidas y cargas innecesarias, manteniendo exactamente el mismo comportamiento funcional y visual.

## 2. Diagnostico previo

| Grupo | Consulta o carga | Small | Medium | Large | Causa |
| --- | --- | ---: | ---: | ---: | --- |
| Lineas por pedido | `order.lines` indirecto en blockers, prioridad y resumen | 39 duplicadas | 191 duplicadas | 399 duplicadas | Acceso lazy por fila |
| Sugerencias de cliente | `suggest_customer_for_email()` por email | 21 duplicadas | 141 duplicadas | 301 duplicadas | Tres consultas por elemento |
| Adjuntos de email | `email.attachments` para indicadores de lista | 1 duplicada | 1 duplicada | 1 duplicada | Carga innecesaria de coleccion completa |

## 3. N+1 confirmados

| Entidad base | Relacion | Punto de acceso | Solucion |
| --- | --- | --- | --- |
| `Order` | `lines` | Bloqueadores, estado y resumen operacional | Metadatos agregados por pedido |
| `Email` | sugerencia de cliente | Render de workbench por email | Mapas compartidos por request |
| `Email` | adjuntos | Indicadores visuales de lista | Flags y no colecciones completas |

## 4. Alcance ejecutado

- Se redujo la carga de home y workbench a los campos y relaciones realmente visibles.
- Se sustituyo el acceso repetido a lineas por metadatos agregados.
- Se reutilizo un mapa comun de sugerencias de cliente en workbench.
- Se eliminaron cargas de adjuntos completos para indicadores simples.
- Se añadió un test de presupuesto de consultas para home y workbench.
- Se rerun el benchmark en `small`, `medium` y `large`.
- Se actualizo la documentacion de rendimiento y el registro tecnico.

## 5. Alcance no ejecutado

- No se rediseño la interfaz.
- No se tocaron `orders` ni `orders/{id}` fuera de lo necesario para home y workbench.
- No se añadió caché.
- No se añadieron indices.
- No se crearon migraciones.
- No se modifico el modelo de datos.
- No se añadió frontend asyncrono nuevo.

## 6. Resultados

| Escenario | Home antes | Home despues | Workbench antes | Workbench despues |
| --- | --- | --- | --- | --- |
| Small | 3.06 ms, 50 queries, 39 duplicadas | 2.10 ms, 11 queries, 1 duplicada | 0.82 ms, 33 queries, 21 duplicadas | 0.29 ms, 11 queries, 1 duplicada |
| Medium | 8.41 ms, 202 queries, 191 duplicadas | 2.72 ms, 11 queries, 1 duplicada | 3.72 ms, 153 queries, 141 duplicadas | 0.63 ms, 11 queries, 1 duplicada |
| Large | 15.69 ms, 410 queries, 399 duplicadas | 2.99 ms, 11 queries, 1 duplicada | 8.33 ms, 313 queries, 301 duplicadas | 1.02 ms, 11 queries, 1 duplicada |

## 7. Validaciones

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_home_workbench_optimization`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_performance_instrumentation`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=development ./.venv/bin/python -m compileall app`
- `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large`

## 8. Decisiones y limitaciones

- Se priorizo eliminar el patron estructural de consultas repetidas antes que tocar otras pantallas.
- `orders`, `history` y `channels` siguen siendo rutas costosas fuera del alcance de esta fase.
- La optimizacion se apoyo en agregacion SQL y reutilizacion de contexto, no en caches ni indices.
- Si cambian las reglas de clasificacion de lineas o sugerencias, la logica agregada debera sincronizarse con detalle.
