# Alcance funcional operativo

## Objetivo

Anchi se presenta al usuario como un gestor de pedidos multicanal. La navegacion principal prioriza el trabajo diario y oculta modulos tecnicos que siguen disponibles como soporte interno.

## Navegacion principal

- `Pedidos pendientes`: cola operativa de pedidos y entradas que requieren accion humana.
- `Entradas`: bandeja comun de correos, WhatsApp e importaciones manuales.
- `Productos`: catalogo del tenant.
- `Clientes`: base de clientes del tenant.

## Area secundaria

- `Configuracion`: canales, correo, identidad, scoring y parametros del tenant.
- `Conocimiento del cliente`: reglas, correcciones, documentos y aprendizaje revisable dentro de cada ficha de cliente.

## Rutas canonicas

- `/inicio` y `/`: acceso a pedidos pendientes.
- `/entries`: entradas recibidas por cualquier canal.
- `/entries/{entry_id}`: resolucion de una entrada concreta.
- `/entries/{entry_id}/process`: procesar una entrada.
- `/entries/{entry_id}/resolve`: resolver una entrada.
- `/imports/manual`: alta manual de entrada Email o WhatsApp.
- `/knowledge`: redireccion de compatibilidad hacia clientes en vista de conocimiento.

## Reglas de producto

- No se crea un segundo sistema de pedidos.
- `Email` e `InboundMessage` siguen alimentando la misma cola operativa.
- `Conversation` se usa para agrupar contexto multicanal cuando existe.
- Las rutas legacy permanecen disponibles para compatibilidad, pero no aparecen en la navegacion principal.
