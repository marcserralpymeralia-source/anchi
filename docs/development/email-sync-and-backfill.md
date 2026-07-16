# Correo IMAP y backfill controlado

## Objetivo

Mantener la sincronizacion IMAP como flujo estable, con checkpoints persistentes por `UID` y `UIDVALIDITY`, deduplicacion por mensaje y posibilidad de reanudar backfill sin volver a procesar lo ya guardado.

## Criterios operativos

- El estado de sincronizacion vive en master.
- Cada ejecucion guarda el ultimo `UID` confirmado.
- Los duplicados se ignoran sin romper el lote.
- El backfill puede pausarse, reanudarse o cancelarse.
- El worker usa el mismo estado que la UI y los jobs.

