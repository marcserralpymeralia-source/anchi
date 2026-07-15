# Runbook de upgrade de esquema

Este runbook resume el flujo seguro para migraciones de base de datos en Anchi.

## 1. Report

```bash
python -m scripts.schema_migrations report-master
python -m scripts.schema_migrations status-all-tenants --summary
python -m scripts.schema_migrations inventory --summary
```

## 2. Backup

- SQLite: copiar el fichero antes de operar.
- PostgreSQL: usar dump o snapshot restaurado en staging.
- Nunca escribir sobre la original en esta fase.

## 3. Dry-run

```bash
python -m scripts.schema_migrations upgrade-master --dry-run
python -m scripts.schema_migrations upgrade-tenant --company-slug demo --dry-run
python -m scripts.schema_migrations upgrade-all-tenants --dry-run
```

## 4. Copia piloto

```bash
python -m scripts.schema_migrations simulate-master --application-version 1.0.0
python -m scripts.schema_migrations simulate-tenant --company-slug demo --application-version 1.0.0
```

La simulacion crea una copia con timestamp, ejecuta `dry-run`, `baseline`, `upgrade` y una segunda ejecucion para confirmar no-op.

## 5. Validacion

- readiness
- login
- pedidos
- worker
- jobs

## 6. Resto de tenants

```bash
python -m scripts.schema_migrations simulate-all-tenants --application-version 1.0.0
```

Cuando toque la ejecucion real, se hace tenant por tenant y con validacion entre pasos.

## 7. Rollback

- Restaurar la copia o snapshot anterior.
- No existe downgrade automatico en esta fase.
- No borrar la base original para "reintentar".
