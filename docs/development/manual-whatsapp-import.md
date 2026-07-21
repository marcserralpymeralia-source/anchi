# Importacion manual de WhatsApp

## Objetivo

Permitir pegar una conversacion o subir un archivo de texto para revisarla sin salir de la app y sin depender de una importacion automatica.

## Flujo

1. El usuario abre `/imports/whatsapp`.
2. Pega la conversacion o sube un `.txt`.
3. La app parsea la conversacion y muestra una vista previa tipo chat.
4. El usuario pulsa `Procesar conversación` para ver scoring y propuesta.
5. El usuario pulsa `Confirmar e importar` para registrar la entrada en `InboundMessage` y encolar el pipeline.
6. La revision de pedido reutiliza la conversacion importada como entrada operativa.

## Contrato guardado

- Texto original.
- Conversacion parseada.
- Participantes detectados.
- Hash estable para deduplicacion.
- Conversacion y mensajes relacionados.

## Decisiones tecnicas

- Guardar la importacion manual como `provider = manual_import`.
- Reutilizar el mismo pipeline que usa el resto de entradas.
- Mostrar la conversacion como chat para facilitar la validacion humana.
- Tratar la vista previa como un paso sin persistencia.

## Riesgos pendientes

- El parser sigue siendo heuristico y puede necesitar ajustes segun el export real de WhatsApp.
- El formato de texto pegado puede perder contexto si el usuario recorta lineas de la conversacion original.
