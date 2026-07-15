# Mensajes y conversaciones

## Objetivo

Unificar la recepcion multicanal bajo un contrato comun de mensaje y una entidad clara de conversacion, manteniendo a `Email` como adaptador inicial y sin duplicar el sistema de pedidos.

## Modelo comun

- `InboundMessage` actua como contrato persistido de mensaje normalizado.
- `Conversation` agrupa mensajes relacionados por canal, proveedor y hilo externo.
- `Order` puede apuntar a una conversacion, pero no define el hilo.

## Canal y proveedor

- Canal funcional: `email`, `whatsapp`, `manual`, `api`.
- Proveedor concreto: `imap`, `gmail`, `microsoft`, `meta`, `manual`.
- El dominio operativo no depende del proveedor para decidir el flujo del pedido.

## Identidad y deduplicacion

- La deduplicacion se basa en `tenant + channel + provider + external_id`.
- Para email, `Message-ID` y el identificador IMAP derivan en `external_id`.
- Si hay hilo externo, la conversacion reutiliza `external_thread_id`.
- La migracion bloquea la promocion si detecta duplicados que impidan la unicidad.

## Contenido y adjuntos

- El mensaje conserva remitente, destinatarios, asunto, cuerpo y metadatos originales.
- `raw_payload_json` guarda cabeceras o metadatos variables.
- Los adjuntos se enlazan al mensaje y reutilizan el mismo almacenamiento fisico.

## Email como adaptador

1. IMAP obtiene el correo.
2. Se normaliza en un mensaje comun.
3. Se busca o crea la conversacion.
4. Se persiste el `InboundMessage`.
5. Se enlazan adjuntos.
6. El pipeline de pedidos consume el mensaje comun.

## Relaciones

- Un mensaje pertenece a una conversacion.
- Una conversacion puede agrupar varios mensajes.
- Una conversacion puede terminar vinculada a cero, uno o varios pedidos.
- Un pedido puede quedarse sin conversacion cuando es manual.

## Compatibilidad

- `Email` se mantiene como capa legacy de entrada y vista operativa.
- No se ha implementado WhatsApp.
- No se ha cambiado la logica de IA.
- No se ha rehecho el sistema de pedidos.

