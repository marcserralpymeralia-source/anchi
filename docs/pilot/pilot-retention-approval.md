# Aprobación humana de retención y offboarding del piloto

Estado: **PENDING HUMAN APPROVAL**

Fecha de propuesta: 2026-10-01  
Revisión técnica Block 13: 2026-10-01  
Ámbito: primer piloto Catalog-only de ANCHI-GEMAVI  
Datos usados en esta validación: únicamente sintéticos

## Decisión que debe aprobarse

La persona responsable de Seguridad/Legal y la persona responsable de Operaciones deben aprobar por escrito la matriz de retención, exportación, anonimización, borrado y revocación de accesos antes de cargar datos reales. Este documento es una propuesta técnica; no fija por sí mismo plazos legales ni autoriza el piloto.

## Tratamiento técnico propuesto

| Clase de dato | Ubicación | Tratamiento durante el piloto | Acción de offboarding | Plazo/criterio final |
|---|---|---|---|---|
| Pedidos, clientes, productos y mensajes | Base tenant | Conservar para operación, soporte y trazabilidad | Congelar escritura operativa; conservar hasta decisión aprobada | **Por aprobar** |
| Entregas, intentos, jobs y errores | Base tenant | Conservar para recovery, auditoría y reconciliación | Pausar trabajos pendientes; conservar histórico | **Por aprobar** |
| Audit logs y correlation IDs | Base tenant/master | Conservar para evidencia y seguridad | No borrar como efecto automático del offboarding | **Por aprobar** |
| Secret references | Base tenant | Conservar sólo la referencia al gestor externo; nunca el secreto en esta salida | Revocar el secreto en el gestor externo y registrar resultado | **Por aprobar** |
| Adjuntos y exports | Storage privado del proveedor | No activar hasta validar storage durable, acceso por tenant y política de PII | Revocar acceso; exportar/borrar/anonimizar según política aprobada | **Por aprobar** |
| Import previews y temporales | Filesystem/storage temporal | TTL y limpieza con dry-run; no son copia de negocio | Limpiar sólo tras comprobación de no dependencia | **Por aprobar** |
| Backups | Servicio de backup gestionado | Cifrados, fuera del runtime, con checksum y prueba de restore | Aplicar expiración del backup según política aprobada | **Por aprobar** |

## Garantías implementadas

- Offboarding desactiva canales, conexiones y rutas; pausa trabajos y entregas pendientes; conserva pedidos, entregas, intentos y auditoría.
- El endpoint no muestra secretos y devuelve únicamente referencias externas para revocación.
- La acción es idempotente y deja rastro `company.offboard` en master y `tenant.offboard` en tenant.
- No se implementa borrado destructivo automático del tenant como parte de este bloque.

## Revisión técnica Block 13

- La preservación de pedidos, mensajes, deliveries, jobs, audit y referencias externas quedó verificada en la rehearsal sintética.
- El filesystem temporal no se acepta como almacenamiento de retención del piloto; falta el storage durable del runtime final.
- No se ha definido ni inferido ningún plazo legal, RPO/RTO o regla de borrado.
- La revocación de secretos depende de un gestor externo que no está conectado en este worktree.

El estado permanece **PENDING HUMAN APPROVAL**. Esta revisión técnica no equivale a aprobación de Marc, Seguridad/Legal u Operaciones.

## Aprobaciones requeridas

| Rol | Nombre | Decisión | Fecha | Firma/enlace |
|---|---|---|---|---|
| Seguridad/Legal | ____________________ | PENDING HUMAN APPROVAL | __________ | ____________________ |
| Operaciones/Owner del piloto | ____________________ | PENDING HUMAN APPROVAL | __________ | ____________________ |

## Condición de salida

No se aceptan datos reales ni se declara GO PILOT hasta que los responsables completen esta matriz, definan los plazos y confirmen el tratamiento de backups, logs, storage y revocación externa.
