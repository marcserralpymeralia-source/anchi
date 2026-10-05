# Gestión formal de esquema y migraciones

Anchi usa un ledger formal de esquema por base de datos:

- `master.db` mantiene su propio `schema_migrations`.
- cada tenant mantiene el suyo en su base operativa.

## Principios

- Las migraciones se ejecutan de forma explícita.
- El arranque de Web y Worker sólo comprueba conexión y ledger; no crea bases, objetos, tablas ni aplica migraciones.
- Si master o cualquier tenant activo no tiene el checksum/versión esperados, Web y Worker fallan cerrados antes de servir o procesar trabajo.
- `schema_migrations` guarda el estado actual verificado, no una configuración implícita.
- No se aceptan URLs arbitrarias de base de datos desde CLI.
- En despliegues con roles separados, el proceso migrador y el runtime usan credenciales distintas. El runtime no recibe permisos DDL.

## Comandos

Desde `backend/`:

```bash
python -m scripts.schema_migrations report-master
python -m scripts.schema_migrations status-all-tenants --summary
python -m scripts.schema_migrations inventory --summary
python -m scripts.schema_migrations upgrade-master --dry-run
python -m scripts.schema_migrations upgrade-master --application-version 1.0.0
python -m scripts.schema_migrations report-tenant --company-slug demo
python -m scripts.schema_migrations upgrade-tenant --company-slug demo --dry-run
python -m scripts.schema_migrations upgrade-tenant --company-slug demo --application-version 1.0.0
python -m scripts.schema_migrations upgrade-all-tenants --dry-run
python -m scripts.schema_migrations simulate-master --application-version 1.0.0
python -m scripts.schema_migrations simulate-tenant --company-slug demo --application-version 1.0.0
python -m scripts.schema_migrations simulate-all-tenants --application-version 1.0.0
```

## Flujo de despliegue: migrar → verificar → arrancar

Ejecutar desde `backend/`, apuntando exclusivamente a las bases de staging sintéticas. Las variables con credenciales no se pasan como argumentos ni se imprimen:

```bash
python -m scripts.schema_migrations upgrade-master --application-version <COMMIT_O_VERSION>
python -m scripts.schema_migrations report-master
python -m scripts.schema_migrations upgrade-tenant --company-id <PILOT_COMPANY_ID> --application-version <COMMIT_O_VERSION>
python -m scripts.schema_migrations report-tenant --company-id <PILOT_COMPANY_ID>
python -m scripts.schema_migrations status-all-tenants --summary
```

Sólo después de que todos los tenants activos indiquen `current`, iniciar Web en su servicio:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Iniciar Worker en su servicio separado, también después de verificar el schema:

```bash
python -m app.workers.jobs_worker
```

`MASTER_MIGRATION_DATABASE_URL` y `TENANT_MIGRATION_DATABASE_URL` son overrides del CLI para URLs con privilegios de migración. En `APP_ENV=production` (incluido el staging aislado, porque `Settings` no admite `APP_ENV=staging`), ambos son obligatorios para sus respectivos upgrades; el CLI comprueba que apunten al mismo host/puerto/database que runtime y exige un usuario DB distinto. Nunca reutiliza silenciosamente la identidad runtime para DDL en esos entornos. `upgrade-all-tenants` está deshabilitado en production: migrar cada tenant explícitamente con su override. En desarrollo/test se conserva el fallback local. Las URLs se suministran por el gestor de secretos del operador, nunca por flags ni documentación.

El comando tenant resuelve `--company-id`/`--company-slug` mediante el registry master con credenciales runtime y aplica cambios a la URL migradora sólo tras verificar que pertenece al mismo destino. La migración no crea la base PostgreSQL ni roles del proveedor: proyecto, database y roles deben existir previamente.

El alta asíncrona de una compañía escribe únicamente el registro/cola master; no crea el archivo SQLite, database PostgreSQL ni objetos tenant. En el flujo operador, crear el registro, ejecutar `upgrade-tenant --company-id <id>` con el migrador dedicado y verificar el reporte. El Worker puede reintentar la inicialización de datos tenant sin DDL hasta que el ledger esté current; entonces completa actores/roles y activa la compañía. Compañías aún inactivas/provisioning no bloquean startup/readiness; todos los tenants activos sí deben tener esquema current. La ruta síncrona de servicio exige una base tenant preexistente y migrada.

## Matriz de privilegios propuesta para PostgreSQL

Esta matriz es una propuesta para el owner del entorno; no implica que se haya aplicado en Neon.

| Identidad | Alcance | Derechos mínimos propuestos | No conceder |
|---|---|---|---|
| `anchi_runtime_master` | `anchi_master` | `CONNECT`; `USAGE` en el schema de aplicación; `SELECT/INSERT/UPDATE/DELETE` en tablas de aplicación; `USAGE/SELECT/UPDATE` en secuencias usadas por esas tablas | Ownership, `CREATE` en schema, `CREATEDB`, `CREATEROLE`, acceso a tenant DB |
| `anchi_runtime_tenant` | `anchi_tenant_pilot` | Mismos derechos DML y de secuencia, sólo en objetos de esa DB | Ownership/DDL, `CREATEDB`, `CREATEROLE`, acceso a master DB |
| `anchi_migrator_master` | `anchi_master` | `CONNECT`; ownership (o membresía controlada del rol owner) sobre el schema y objetos master para ejecutar `CREATE/ALTER` de tablas, columnas, índices y ledger | `CREATEDB`, `CREATEROLE`, acceso a tenant DB si no migra tenants |
| `anchi_migrator_tenant` | DB tenant explícita | Mismos derechos DDL/ownership limitados a los objetos de esa DB tenant | `CREATEDB`, `CREATEROLE`, acceso a otras DBs |

Las migraciones versionadas inspeccionadas usan DDL de tablas/columnas/índices y ledger; no se encontraron instrucciones `CREATE EXTENSION`, `CREATE ROLE`, `CREATE DATABASE`, funciones ni triggers. Confirmar con el owner y repetir la inspección al añadir una migración. PostgreSQL no permite sustituir ownership de objetos existentes por un simple `GRANT CREATE`; la asignación/transferencia inicial de ownership requiere decisión y ejecución separada del owner. No se ejecutó SQL de grants.

## Pools runtime PostgreSQL

`DATABASE_POOL_SIZE=3`, `DATABASE_MAX_OVERFLOW=1` y `DATABASE_POOL_TIMEOUT_SECONDS=10` son defaults configurables, con límites validados. Se aplican a los engines runtime master y tenant con `pool_pre_ping`; SQLite conserva defaults de dialecto. Para un piloto de un master + un tenant, una instancia Web y una Worker tienen un techo teórico de `2 procesos × 2 DBs × (3 + 1) = 16` conexiones, antes de herramientas one-shot, por debajo del límite de 112. No se activa Neon pooler en código. Recalcular si se añaden tenants, réplicas, Cron con acceso directo a DB o procesos adicionales.

## Baseline

Si una base ya existe y el esquema está validado manualmente, se puede registrar el estado actual con `--baseline`.

## Inventario y simulación

La fase 7.1 introduce un inventario de las bases visibles desde `master.db` y una simulación sobre copias en `backend/storage/migration-simulations/`.

- `status-all-tenants --summary` muestra master + tenants conocidos y el estado detectado.
- `inventory --summary` añade las bases SQLite descubiertas en el repositorio y marca las que requieren revisión manual.
- `simulate-master`, `simulate-tenant` y `simulate-all-tenants` copian primero la base, luego ejecutan `dry-run`, `baseline`, `upgrade` y una segunda ejecución para confirmar no-op.

## Backup y rollback

Antes de ejecutar upgrades en una base real:

1. Hacer copia física de `master.db` y de la base tenant afectada.
2. Ejecutar `--dry-run`.
3. Ejecutar el upgrade explícito.
4. Si algo falla, restaurar la copia anterior.

Esta fase no introduce rollback automático porque el criterio seguro es restaurar desde backup cuando una migración estructural no deja la base en el estado esperado.
