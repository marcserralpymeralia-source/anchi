# Flujo de resolucion de pedidos

## Objetivo

Mantener una unica ruta operativa para resolver entradas de pedido sin separar el comportamiento entre correo, WhatsApp o importacion manual.

## Flujo funcional

1. La entrada llega a `channels` o a `imports`.
2. El usuario abre el elemento y pulsa `Procesar` o `Resolver`.
3. La app dirige al detalle operativo correcto:
   - si ya existe pedido, abre `/orders/{id}`;
   - si no existe, encola el trabajo de procesamiento.
4. La pantalla de revision muestra en una sola vista:
   - la entrada recibida,
   - el correo o chat original,
   - la propuesta del agente,
   - cliente y lineas sugeridas,
   - acciones de validacion.
5. El usuario corrige cliente y lineas, valida y confirma.
6. El pedido queda creado, actualizado o listo para exportacion segun el estado.

## Decisiones tecnicas

- Reutilizar la pantalla de revision de pedido como destino util para la resolucion.
- No duplicar la logica de acciones entre email e inbound message.
- Mantener el preview de WhatsApp como chat, no como texto plano.
- Mantener el adjunto y el correo dentro de la misma pantalla de revision.

## Riesgos pendientes

- Si la entrada no tiene pedido asociado, el usuario depende del job encolado para completar la resolucion.
- Los imports manuales dependen de que el texto pegado preserve un formato suficientemente legible.
