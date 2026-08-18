# Bandeja de entradas `/entries`

## Objetivo

`/entries` es la bandeja operativa canonica para todo lo recibido por el tenant: correo IMAP persistido, WhatsApp e importaciones manuales.

La pantalla no conecta con IMAP ni procesa IA durante un `GET`. Solo lee datos ya guardados en la base tenant.

## Rutas

| Ruta | Metodo | Uso |
|---|---|---|
| `/entries` | `GET` | Bandeja canonica autenticada |
| `/entries/sync` | `POST` | Encola un job `email_sync` idempotente |
| `/entries/{entry_id}` | `GET` | Abre la revision o vuelve a la bandeja enfocada |
| `/entries/{entry_id}/resolve` | `GET` | Resolver entrada |
| `/entries/{entry_id}/process` | `POST` | Encolar procesamiento como pedido |
| `/channels` | `GET` | Legacy: redirige 303 a `/entries` |
| `/history` | `GET` | Legacy: redirige 303 a `/entries?tab=processed&date_range=30d` |

## Contrato de datos

- Emails: `Email`, `EmailAttachment`, `Order`.
- WhatsApp y otros canales: `InboundMessage`, `MessageAttachment`, `InputChannel`, `Order`.
- La union de bandeja siempre filtra por `company_id` del usuario autenticado.
- IDs externos coincidentes entre tenants no autorizan acceso cruzado.

## Sincronizacion

`POST /entries/sync` no descarga correos dentro de la request. Crea o reutiliza un job `email_sync` con payload seguro:

```json
{
  "auto_process": false,
  "unread_only": false
}
```

El worker es responsable de conectar a IMAP, deduplicar, persistir y actualizar checkpoints.

## UI

- La bandeja muestra canal, estado, confianza, remitente, asunto, resumen, adjuntos y pedido asociado.
- WhatsApp se representa como conversacion cuando el payload incluye mensajes estructurados.
- Los adjuntos se previsualizan mediante rutas internas protegidas por tenant.

## Validaciones

Cobertura principal en `backend/tests/test_entries_inbox.py`:

- autenticacion y retorno a `/entries`;
- membresia inactiva sin bucle de login;
- redirecciones legacy;
- aislamiento tenant;
- encolado idempotente de sync y procesamiento;
- listado de email, WhatsApp y adjuntos;
- filtros, paginacion y ausencia de llamadas IMAP en `GET /entries`.
