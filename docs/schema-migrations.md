# Gestión formal de esquema y migraciones

Anchi usa un ledger formal de esquema por base de datos:

- `master.db` mantiene su propio `schema_migrations`.
- cada tenant mantiene el suyo en su base operativa.

## Principios

- Las migraciones se ejecutan de forma explícita.
- El arranque del servidor no aplica migraciones.
- `schema_migrations` guarda el estado actual verificado, no una configuración implícita.
- No se aceptan URLs arbitrarias de base de datos desde CLI.

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
