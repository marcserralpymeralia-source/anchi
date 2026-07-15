# Home and workbench performance

## Objetivo

Dejar documentado como se carga la pantalla principal y la workbench despues de la Fase 6B, con foco en reducir consultas repetidas sin cambiar comportamiento visible.

## Arquitectura de carga

La carga inicial usa un flujo simple:

1. contexto de tenant y usuario ya resuelto;
2. consulta principal de orders o emails visibles;
3. consulta agregada de lineas por pedido;
4. mapa compartido de sugerencias de cliente en workbench;
5. composicion final de items para Jinja;
6. render SSR sin acceso lazy por fila.

## Consultas principales

- listado base de home o workbench;
- metadatos agregados de lineas por pedido;
- sugerencias de cliente agrupadas para emails de workbench;
- contadores visibles ya preparados en el servicio;
- contexto compartido de tenant, usuario y settings validado una vez.

## Paginacion

- La paginacion visible se mantiene.
- La primera pagina se resuelve en SQL.
- No se cargan colecciones completas para luego recortar en Python.
- No se introduce una nueva UX de paginacion.

## Relaciones permitidas

Solo se cargan relaciones realmente visibles en la pantalla inicial:

- `email`
- `customer`
- `validated_customer`

No se cargan `lines` ni adjuntos completos para contar o clasificar elementos.

## Contadores y contexto compartido

- Los contadores visibles se preparan una sola vez por request.
- El contexto de tenant no se recalcula por cada fila.
- Las sugerencias de cliente se reutilizan en bloque durante el render.

## Reglas para evitar N+1

- No acceder a `order.lines` desde la plantilla.
- No consultar sugerencias de cliente por cada email.
- No usar `len()` sobre colecciones lazy para mostrar metadatos.
- No resolver attachments completos si solo se necesita un indicador.

## Metricas antes y despues

| Escenario | Endpoint | Antes | Despues |
| --- | --- | --- | --- |
| Small | `/` | 3.06 ms, 50 queries, 39 duplicadas | 2.10 ms, 11 queries, 1 duplicada |
| Small | `/workbench` | 0.82 ms, 33 queries, 21 duplicadas | 0.29 ms, 11 queries, 1 duplicada |
| Medium | `/` | 8.41 ms, 202 queries, 191 duplicadas | 2.72 ms, 11 queries, 1 duplicada |
| Medium | `/workbench` | 3.72 ms, 153 queries, 141 duplicadas | 0.63 ms, 11 queries, 1 duplicada |
| Large | `/` | 15.69 ms, 410 queries, 399 duplicadas | 2.99 ms, 11 queries, 1 duplicada |
| Large | `/workbench` | 8.33 ms, 313 queries, 301 duplicadas | 1.02 ms, 11 queries, 1 duplicada |

## Checklist para futuros cambios

- Revisar que no reaparezcan consultas por fila.
- Mantener el conteo de lineas fuera de la plantilla.
- Mantener las sugerencias de cliente en un unico lookup por request.
- Validar small, medium y large cada vez que cambie la composicion de home o workbench.
- No ampliar el alcance a orders, history o channels sin una fase propia.
