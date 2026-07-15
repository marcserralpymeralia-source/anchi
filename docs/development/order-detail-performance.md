# Rendimiento del detalle de pedido

## Objetivo

La Fase 6D reduce el coste de la pantalla de revision de pedido (`/orders/{order_id}`) sin perder la capacidad operativa de ver cliente, lineas, adjuntos, scoring y acciones.

## Situacion previa

Antes de la optimizacion, el detalle completo de pedido cargaba demasiadas relaciones ORM y dependencias lazy:

- cliente completo para cabecera y sugerencias
- catalogo completo de productos para matching
- adjuntos y contexto de correo con mas peso del necesario
- snapshot de cabecera poco estable para renderizacion

Eso hacia que la pantalla de detalle arrastrara cientos de registros en escenarios sinteticos grandes.

## Arquitectura final

La version final del detalle usa:

- `load_only` para el pedido base
- snapshot ligero del cliente para la cabecera
- helper compartido para candidatos de cliente y producto
- render Jinja sin acceso a relaciones lazy de alto coste
- carga minima de adjuntos y contexto visible

La idea no es ocultar informacion, sino cargar solo lo necesario para que la revision sea rapida.

## Presupuesto objetivo

| Indicador | Presupuesto |
| --- | --- |
| Consultas SQL | <= 15 |
| Consultas duplicadas | <= 2 |
| Tamano de respuesta | <= 140 KB |
| Registros cargados | <= 108 |

## Resultado medido

| Escenario | Duracion | Consultas | Duplicadas | SQL | Jinja | Respuesta | Registros | Estado |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Small | 1.71 ms | 14 | 0 | 1.06 ms | 0.67 ms | 30262 bytes | 3 | OK |
| Medium | 16.20 ms | 14 | 0 | 15.48 ms | 0.70 ms | 30262 bytes | 3 | OK |
| Large | 66.40 ms | 14 | 0 | 65.69 ms | 0.71 ms | 30262 bytes | 3 | OK |

## Escalado sintetico

| Escenario | Clientes | Productos | Alias totales | Consultas | Registros cargados |
| --- | --- | --- | --- | --- | --- |
| Small | 20 | 20 | 10 | 14 | 3 |
| Medium | 160 | 380 | 54 | 14 | 3 |
| Large | 380 | 980 | 126 | 14 | 3 |

## Reglas de diseño tecnico

- No volver a cargar catalogos completos en el detalle.
- No acceder a relaciones lazy desde Jinja.
- No embutir binarios ni previews pesadas por defecto.
- Reutilizar siempre el helper compartido de candidatos.
- Mantener el snapshot de cabecera como fuente visible estable.

