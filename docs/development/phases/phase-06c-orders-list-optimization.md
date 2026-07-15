# Fase 6C - Optimizacion del listado general de pedidos

## 1. Objetivo

Optimizar el listado general de pedidos para que muestre solo el resumen operativo visible, con paginacion real, menos consultas y mucho menos HTML generado por request.

## 2. Diagnostico previo

| Problema | Efecto |
| --- | --- |
| Modal completo embebido por pedido | HTML masivo y render lento |
| `order.lines` cargado en la lista | Consultas y memoria crecientes por fila |
| `email.attachments` cargado en la lista | Trabajo innecesario para un indicador simple |
| Exportaciones y formularios completos por fila | Mezcla de listado y detalle en la misma pantalla |

## 3. Alcance ejecutado

- Se mantuvo la paginacion SQL real.
- Se redujo el listado a las relaciones estrictamente visibles.
- Se calcularon alertas por lote con agregados por pagina.
- Se elimino el detalle pesado embebido por fila.
- Se dejo el acceso completo al detalle en `/orders/{order_id}`.
- Se añadio cobertura de presupuesto de consultas y tamano de respuesta.
- Se rerun el benchmark en `small`, `medium` y `large`.

## 4. Alcance no ejecutado

- No se toco el detalle completo de pedido.
- No se añadieron caches ni indices.
- No se cambiaron modelos ni migraciones.
- No se rediseño la UX del detalle de pedido.

## 5. Resultados

| Escenario | Antes | Ahora |
| --- | --- | --- |
| Small | 21.00 ms, 37 queries, 455,245 bytes | 4.35 ms, 14 queries, 56,511 bytes |
| Medium | 408.57 ms, 42 queries, 4,407,796 bytes | 2.62 ms, 14 queries, 78,266 bytes |
| Large | 762.54 ms, 42 queries, 10,977,304 bytes | 6.91 ms, 14 queries, 97,757 bytes |

## 6. Control medium

| Endpoint | Variacion |
| --- | --- |
| `/` | +6.6% |
| `/workbench` | +1.6% |
| `/channels?tab=processed&date_range=30d` | -1.1% |
| `/history?date_range=90d` | -50.1% |
| `/jobs/monitor` | -76.3% |

## 7. Validaciones

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_orders_list_optimization tests.test_home_workbench_optimization tests.test_performance_instrumentation`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium`
- `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large`

## 8. Riesgos y siguientes pasos

- `channels` sigue siendo una ruta costosa de forma estructural, aunque la variacion quedo dentro del margen esperado.
- `history` sigue siendo una ruta pesada fuera de esta fase.
- La siguiente mejora natural seria aplicar el mismo patron de resumen por lote a otras pantallas de lista grandes.
