# Flujo de revision de pedido

## Objetivo

Definir la pantalla de revision como el punto unico donde el operador compara la entrada recibida con la propuesta del agente y decide si valida, corrige, reprocesa, exporta o descarta.

## Pantalla funcional

La vista debe mantener dos zonas principales:

- Columna izquierda: entrada original recibida, incluyendo correo, conversacion de WhatsApp, texto pegado, documento o adjuntos disponibles.
- Columna derecha: propuesta del agente, con cliente sugerido, lineas interpretadas, incidencias, scoring y acciones principales.

La comparacion debe permitir actuar en pocos segundos sin obligar al usuario a navegar por pantallas tecnicas.

## Rutas canonicas

- `/entries/{entry_id}/resolve`: abre la revision si ya existe un pedido o prepara la entrada para procesarla.
- `/entries/{entry_id}/process`: encola el procesamiento de la entrada cuando aun no hay pedido operativo.
- `/orders/{order_id}/save`: guarda cambios manuales de la propuesta.
- `/orders/{order_id}/reprocess`: vuelve a lanzar interpretacion del agente.
- `/orders/{order_id}/validate`: confirma la propuesta revisada.
- `/orders/{order_id}/export`: envia el pedido al circuito de salida.
- `/orders/{order_id}/discard`: marca la entrada como descartada cuando no procede.

## Reglas de uso

- El canal de entrada no debe cambiar el flujo de revision.
- El LLM propone, la aplicacion valida y el operador confirma.
- Las acciones tecnicas deben quedar fuera de la vista principal salvo que sean necesarias para resolver el pedido.
- Las rutas legacy pueden redirigir o delegar, pero no deben crear un flujo paralelo.
