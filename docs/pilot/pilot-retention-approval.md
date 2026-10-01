# Aprobación humana de retención y offboarding del piloto

Estado: **APPROVED FOR PILOT**

Fecha de propuesta: 2026-10-01  
Revisión técnica Block 13: 2026-10-01  
Ámbito: primer piloto Catalog-only de ANCHI-GEMAVI  
Datos usados en esta validación: únicamente sintéticos

## Decisión del owner

| Pilot Owner | Decision | Date |
|---|---|---|
| Marc Serral | APPROVED | 2026-10-01 |

Esta es la única aprobación humana registrada en este documento. No se inventan aprobadores ni firmas de terceros.

## Alcance de la política

Estas duraciones son política operacional del primer piloto; no declaran que cada plazo venga impuesto legalmente. Las instrucciones contractuales del responsable del tratamiento pueden sustituirlas cuando ANCHI trate datos por cuenta del cliente.

## Retención piloto aprobada

| Categoría | Retención piloto aprobada |
|---|---|
| Pedidos canónicos | Durante el piloto + 90 días después de offboarding, salvo instrucción contractual distinta |
| Clientes/productos replicados y mappings | Mientras tenant activo + 90 días después de offboarding |
| Mensajes WhatsApp de origen | 90 días después de procesamiento correcto |
| Deliveries, attempts, jobs y errores | 12 meses |
| AuditLog y correlation IDs | 12 meses |
| Attachments/documentos | 30–90 días después de procesamiento, según necesidad operativa/contractual |
| Temporales/previews | 24–72 horas |
| Backups | Rotación inicial de 30 días |
| Secret references | Mientras exista la conexión; retirar referencia después de revocación confirmada |
| Datos sujetos a reclamación/obligación de conservación | `BLOCKED/LEGAL HOLD` durante el periodo aplicable |

## Garantías implementadas

- Offboarding desactiva canales, conexiones y rutas; pausa trabajos y entregas pendientes; conserva pedidos, entregas, intentos y auditoría.
- El endpoint no muestra secretos y devuelve únicamente referencias externas para revocación.
- La acción es idempotente y deja rastro `company.offboard` en master y `tenant.offboard` en tenant.
- No se implementa borrado destructivo automático del tenant como parte de este bloque.

## Principios operativos aprobados

### ANCHI como sistema operativo intermedio

Cuando ANCHI trate datos por cuenta del cliente, la política definitiva podrá quedar sustituida por las instrucciones contractuales del responsable del tratamiento.

### Offboarding no equivale a borrado

El flujo aprobado es:

```text
ACTIVE
  -> OFFBOARDED / tratamiento activo detenido
  -> RETAINED
  -> BLOCKED / LEGAL HOLD cuando proceda
  -> DELETED cuando corresponda
```

Offboarding detiene nuevas operaciones, pero no destruye automáticamente el histórico.

### Backups

- La eliminación en sistemas activos no requiere manipular individualmente cada backup histórico.
- Los datos desaparecerán de backups mediante su ciclo de rotación.
- Un restore no debe devolver deliberadamente a explotación normal datos que ya debían permanecer suprimidos/bloqueados.
- `pg_dump`/backup de DB no equivale a backup de object storage.

### Safe default

Hasta que exista una instrucción contractual diferente:

- no ejecutar borrado destructivo de Orders;
- no ejecutar borrado destructivo de AuditLog;
- preservar histórico requerido para trazabilidad;
- mantener cleanup técnico de temporales;
- utilizar dry-run antes de cualquier política futura de cleanup masivo.

RPO/RTO no forman parte de esta política de retención; permanecen en continuidad/backup como decisión separada.

## Revisión técnica Block 13

- La preservación de pedidos, mensajes, deliveries, jobs, audit y referencias externas quedó verificada en la rehearsal sintética.
- El filesystem temporal no se acepta como almacenamiento de retención del piloto; falta el storage durable del runtime final.
- Los plazos anteriores son una política operacional aprobada, no una inferencia de obligación legal.
- La revocación de secretos depende de un gestor externo que no está conectado en este worktree.

La política está **APPROVED FOR PILOT** por Marc Serral. Su enforcement automático, storage durable y ejecución en runtime final no están validados; el gate técnico de retención permanece **OPEN**.

## Registro de aprobación

| Rol | Nombre | Decisión | Fecha | Firma/enlace |
|---|---|---|---|---|
| Pilot Owner | Marc Serral | APPROVED | 2026-10-01 | Decisión humana registrada |

## Condición de salida

La aprobación humana no cierra el gate global: el enforcement técnico de retención, storage durable, backups gestionados y revocación externa deben validarse en el runtime final.
