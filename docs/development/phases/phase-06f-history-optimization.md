# Fase 6F - Optimizacion de `/history`

## Objetivo

Reducir el coste de la pantalla de historial para que deje de cargar todo el universo de eventos en memoria y pase a trabajar con una pagina visible real, manteniendo la mezcla operativa de pedidos y correos.

## Diagnostico inicial

`/history` seguia siendo una de las rutas mas pesadas del sistema porque resolvia filtros y ordenacion sobre una coleccion demasiado grande antes de construir la pagina visible. La pantalla funcionaba, pero no estaba preparada para crecer con el volumen real de actividad.

## Alcance ejecutado

- Union SQL de pedidos y correos para el historial.
- Filtros resueltos en base de datos.
- Ordenacion y paginacion en SQL.
- Resumen y contadores agregados separados.
- Carga ORM solo para la pagina visible.
- Reutilizacion de mapas compartidos para sugerencias y metadatos.
- Presupuesto de consultas en test de instrumentacion.

## Fuera de alcance

- No se cambio el comportamiento visible de tabs ni filtros.
- No se rediseño la interfaz de historial.
- No se altero el modelo de dominio de pedidos o correos.

## Cambios principales

1. Se separo el universo del historial en filas SQL ligeras.
2. Se movieron filtros y orden a la base de datos.
3. Se limito la carga ORM a la pagina visible.
4. Se evito recomputar sugerencias y metadatos por fila.
5. Se añadio un control de regresion para el presupuesto de consultas.

## Linea base y resultado

| Escenario | Antes | Despues |
| --- | --- | --- |
| Small | 4.39 ms, 33 queries, 21 duplicadas | 18.74 ms, 19 queries, 3 duplicadas |
| Medium | 6.29 ms, 153 queries, 141 duplicadas | 27.05 ms, 19 queries, 3 duplicadas |
| Large | 25.01 ms, 313 queries, 301 duplicadas | 29.92 ms, 19 queries, 3 duplicadas |

Lectura rapida: el cambio mas importante fue bajar la presion SQL de forma drastica. El tiempo medido sigue siendo sensible al volumen sintetico y al render de la pagina visible, pero la ruta ya no escala con el historial completo.

## Validaciones ejecutadas

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_performance_instrumentation`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=development ./.venv/bin/python -m py_compile app/pages/routes.py`
- `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"`
- Benchmark small, medium y large

## Criterios de aceptacion

- La pantalla responde sin cargar el historial completo en memoria.
- El presupuesto de consultas queda acotado.
- La pagina visible sigue mostrando la mezcla operativa de pedidos y correos.
- Los filtros y el orden siguen funcionando desde la experiencia actual.

## Riesgos residuales

- El coste grande queda ahora concentrado en SQL, asi que el siguiente cuello de botella probable sera la complejidad de filtros en escenarios de mucho volumen.
- El render sigue dependiendo de la composicion de la pagina visible, aunque ya no arrastra colecciones completas.

## Estado final

- Commit funcional: pendiente de crear
- Cierre documental: esta fase
