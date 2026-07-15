# Inventario de bases y estado de migraciones

Generado en la Fase 7.1 a partir de `master.db`, el catalogo de tenants y el descubrimiento de ficheros SQLite del repositorio.

## Inventario principal

| Base logica | Tipo | Estado | Baseline | Upgrade | Bloqueos | Accion |
|---|---|---|---|---|---|---|
| `master` | master | `current-without-ledger` | si, sobre copia | si, sobre copia | ninguno | Baseline inicial y luego upgrade en copia |
| `tenant:demo` | tenant | `legacy-recognized` | si, sobre copia | si, sobre copia | ninguno | Baseline/upgrade en copia antes de tocar original |
| `anchi_demo.db` | tenant | `legacy-recognized` | si, sobre copia | si, sobre copia | ninguno | Mantener como tenant operativo conocido |
| `0002-mulet-hidalgo.db` | tenant | `legacy-recognized` | si, sobre copia | si, sobre copia | ninguno | Mantener como tenant operativo conocido |
| `dialma.db` | tenant | `legacy-recognized` | si, sobre copia | si, sobre copia | ninguno | Mantener como tenant operativo conocido |
| `gemavi.db` | tenant | `legacy-recognized` | si, sobre copia | si, sobre copia | ninguno | Mantener como tenant operativo conocido |
| `order_agent.db` | tenant | `legacy-recognized` | si, sobre copia | si, sobre copia | posible duplicado de archivo | Revisar manualmente antes de una operacion real |

## Lectura operativa

- El master actual no tiene ledger persistido, pero el esquema detectado es coherente y se puede baselinear sobre copia.
- Los tenants conocidos ya tienen un esquema reconocible, aunque con ledger antiguo y sin `job_attempts`.
- La carpeta `backend/storage/migration-simulations/` queda reservada para copias temporales con timestamp.
- No se modificaron las bases originales.

## Orden sugerido

1. Master en copia.
2. Tenant demo en copia.
3. Resto de tenants, uno a uno, con validacion posterior.
4. Revision manual de cualquier archivo SQLite duplicado o ambiguo antes de operar la original.
