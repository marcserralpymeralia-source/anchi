# Organizacion del codigo

## Inventario breve

| Archivo | Responsabilidad | Estado |
|---|---|---|
| `backend/app/db/models.py` | Modelos del tenant y contratos operativos centrales | Se mantiene monolitico por ahora |
| `backend/app/agent/platform.py` | Pipeline comun de interpretacion, scoring, aprendizaje y exportacion | Consolidado |
| `backend/app/settings/integrations.py` | Configuracion IMAP, backfill y prompts | Consolidado |
| `backend/app/workers/jobs_worker.py` | Ejecucion asincrona de jobs | Consolidado |
| `backend/app/migrations/inspection.py` | Inventario, simulacion y diagnostico de esquemas | Limpieza de conexiones y warnings |
| `backend/app/migrations/registry.py` | Registro de migraciones y compatibilidad | Limpieza de conexiones y warnings |
| `backend/app/tenancy/database.py` | Resolucion de tenant y session factory | Limpieza de conexiones y warnings |
| `backend/app/orders/routes.py` | Flujo operativo de pedidos | Sin cambios funcionales en este bloque |
| `backend/app/channels/routes.py` | Bandeja de canales | Se elimino redireccion legacy huérfana |
| `backend/app/customers/routes.py` | CRUD e importacion de clientes | Se elimino redireccion legacy huérfana |
| `backend/app/products/routes.py` | CRUD e importacion de productos | Se elimino redireccion legacy huérfana |

## Decision sobre `db/models.py`

Se reviso la opcion de dividir `backend/app/db/models.py` por dominios, pero se descarta por ahora. La relacion entre pedidos, mensajes, aprendizaje, jobs y configuracion sigue siendo muy cruzada y la particion introduce dependencias circulares o demasiado codigo puente para este bloque. La decision es mantener el archivo monolitico, ordenado por secciones funcionales, hasta que exista una separacion clara con bajo coste.

## Limpieza aplicada

- Se consolidaron varias inspecciones de SQLite para cerrar conexiones correctamente.
- Se eliminaron tres redirecciones legacy sin referencias internas:
  - `GET /products/import`
  - `GET /customers/import`
  - `GET /channels/legacy`

## Riesgo pendiente

El archivo de modelos sigue siendo grande. La deuda no es la longitud en si, sino el acoplamiento entre dominios. Solo deberia fragmentarse cuando exista una frontera tecnica clara.
