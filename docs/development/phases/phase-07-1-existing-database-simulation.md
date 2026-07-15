# Fase 7.1 - Inventario, baseline y simulacion de bases existentes

## Objetivo

Inventariar las bases actuales, clasificar su estado de esquema y probar baseline/upgrade solo sobre copias.

## Resultado

- Se confirmo el inventario desde `master.db`.
- Se separo el catalogo master/tenant de las bases detectadas en disco.
- Se habilito el modo de inspeccion y simulacion sin escritura sobre las bases originales.
- Se añadió la proteccion para que `dry-run` no escriba.
- El worker ahora rechaza tenants con esquema incompatible sin tratarlo como fallo funcional.

## Comandos ejecutados

```bash
python -m scripts.schema_migrations report-master
python -m scripts.schema_migrations status-all-tenants --summary
python -m scripts.schema_migrations inventory --summary
python -m scripts.schema_migrations upgrade-master --dry-run
python -m scripts.schema_migrations upgrade-tenant --company-slug demo --dry-run
python -m scripts.schema_migrations upgrade-all-tenants --dry-run
python -m scripts.schema_migrations simulate-master --application-version 1.2.3
python -m scripts.schema_migrations simulate-tenant --company-slug demo --application-version 1.2.3
python -m scripts.schema_migrations simulate-all-tenants --application-version 1.2.3
```

## Hallazgos clave

| Base | Clasificacion | Observacion |
|---|---|---|
| `master` | `current-without-ledger` | Baseline seguro sobre copia |
| `tenant:demo` | `legacy-recognized` | Ledger antiguo, faltaba `job_attempts` |
| Tenants adicionales | `legacy-recognized` | Requieren la misma secuencia sobre copia |

## Validacion

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_schema_migrations`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_jobs_reliability`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_tenant_isolation`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_observability`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=development ./.venv/bin/python -m compileall app scripts tests`
- `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"`

## Riesgo residual

- Hay ficheros SQLite duplicados en disco que conviene revisar manualmente antes de operar una base original.
- La fase no hace baseline ni upgrade sobre originales; solo deja la operativa lista para la siguiente instruccion.
