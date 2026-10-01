# Entrega de fase — Block 12 Pilot Closure

## 1. Objetivo

Cerrar la preparación técnica y operativa del piloto con evidencia reproducible, sin declarar producción ni usar datos reales.

## 2. Alcance ejecutado

Backup/restore, almacenamiento persistente de prueba, heartbeats, estado operativo, offboarding, guardas de enqueue, pruebas deterministas y documentación de rehearsal. Ver `docs/pilot/block-12-pilot-closure.md` y `docs/pilot/pilot-backup-restore-runbook.md`.

## 3. Alcance no ejecutado

Runtime final con PostgreSQL/Blob, cron/alertas del proveedor, rehearsal multi-proceso en staging, aprobación externa de PII/RBAC y browser final.

## 4. Diagnóstico previo

El baseline `3df6d8b` dejaba abiertos storage persistente, backup/restore, scheduler/worker operables, offboarding, contaminación de suite y aceptación final.

## 5. Cambios realizados

- módulo backup SQLite/PostgreSQL y CLI de rehearsal;
- modelo/migración de heartbeats y status operativo;
- offboarding auditable y bloqueo de nuevo trabajo;
- aislamiento dinámico de previews y fixtures de storage;
- tests de cierre y documentación/runbook.

## 6. Archivos modificados

Consultar `git diff --name-only 3df6d8b..` y `git status --short`; no se tocaron archivos fuera del alcance piloto.

## 7. Archivos creados

- `backend/app/core/backup.py`
- `backend/app/core/operational.py`
- `backend/scripts/pilot_backup_restore.py`
- `backend/tests/test_pilot_closure.py`
- `docs/pilot/pilot-backup-restore-runbook.md`
- `docs/pilot/block-12-pilot-closure.md`

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativa descartada |
|---|---|---|
| PostgreSQL estándar + SQLite aislado | interoperabilidad y tests offline | formato propietario sin verificación |
| heartbeat durable | detectar stale/error tras reinicio | sólo logs in-process |
| offboarding reversible a nivel histórico | preservar trazabilidad y evitar borrado accidental | hard delete |
| resolver storage al operar | evitar raíz congelada entre procesos/tests | constante única de importación |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python3 -m unittest -v backend/tests/test_pilot_closure.py` | 5/5 OK |
| suite dirigida jobs + setup | 35/35 OK |
| suite offline completa | `Ran 681 tests in 248.800s`, 4 fallos clasificados fuera de Catalog-only, 1 skip |

## 10. Tests añadidos o modificados

Añadido `test_pilot_closure.py`; ajustados tests de almacenamiento/entorno para que cada módulo use directorio temporal y no contamine el siguiente.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| backup/restore e invariantes | parcial: SQLite OK, PostgreSQL pendiente | runbook + test |
| storage tras reinicio | parcial: procesos locales OK | test de subprocess |
| worker/scheduler status | parcial: heartbeat local OK | operational status + test |
| offboarding | OK offline | test + endpoint |
| browser final | pendiente | agent-browser no instalado/runtime final no disponible |
| go-live | NO GO | block-12 closure |

## 12. Riesgos y observaciones pendientes

No hay evidencia suficiente para tráfico real: faltan runtime final, alertas, restore PostgreSQL y aprobaciones operativas.

## 13. Desviaciones respecto al alcance inicial

La aceptación browser y la rehearsal de proveedor se dejan explícitamente como validación de runtime, no se simulan como completadas con SQLite.

## 14. Estado final de Git

Debe revisarse con `git status --short`, `git diff --check` y `git diff` antes de crear el commit. No hacer push.

## 15. Recomendación para la siguiente fase

Ejecutar la rehearsal en staging con PostgreSQL, storage persistente, web/worker/scheduler separados, cron y alertas; después repetir browser acceptance y decidir entre `GO` o `NO GO` con acta firmada.
