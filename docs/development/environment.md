# Guia del entorno

## Requisitos

- Python 3.13.3 en el entorno virtual local.
- FastAPI con Jinja, SQLAlchemy y Uvicorn instalados.
- SQLite para desarrollo local por defecto.

## Entorno virtual

- Ruta: `backend/.venv`
- Activacion: `source backend/.venv/bin/activate`
- Python del venv: `backend/.venv/bin/python`

## Instalacion

Desde `backend/`:

```bash
./.venv/bin/python -m pip install -r requirements.txt
```

## Variables necesarias

Sin exponer valores secretos:

- `APP_ENV`
- `APP_NAME`
- `APP_SLUG`
- `SECRET_KEY`
- `ENCRYPTION_KEY`
- `SESSION_COOKIE`
- `SESSION_COOKIE_SECURE`
- `SESSION_COOKIE_SAMESITE`
- `SESSION_MAX_AGE`
- `ALLOWED_HOSTS`
- `CORS_ALLOWED_ORIGINS`
- `DATABASE_URL`
- `MASTER_DATABASE_URL`
- `JOB_POLL_INTERVAL_SECONDS`
- `JOB_MAX_ATTEMPTS`
- `JOB_RETRY_BASE_SECONDS`
- `JOB_RETRY_MAX_SECONDS`
- `JOB_STALE_AFTER_SECONDS`
- `LOG_FORMAT`
- `LOG_LEVEL`
- `PERFORMANCE_PROFILING_ENABLED`
- `DEFAULT_COMPANY_NAME`
- `DEFAULT_ADMIN_EMAIL`
- `DEFAULT_ADMIN_PASSWORD`
- `ENABLE_DEMO_BOOTSTRAP`
- `DEBUG`

Comportamiento por defecto:

- si `APP_ENV` no existe, la aplicacion usa `development` y avisa una vez en logs;
- `development` y `test` permiten valores locales seguros por defecto;
- `production` exige configuracion explicita y rechaza valores demo.

Alias historicas compatibles:

- `ENVIRONMENT` sigue siendo aceptado como alias de `APP_ENV`.
- `APP_SECRET_KEY` sigue siendo aceptado como alias de `SECRET_KEY`.
- `SEED_DEMO_DATA` sigue siendo aceptado como alias de `ENABLE_DEMO_BOOTSTRAP`.

## Base de datos de desarrollo

- Base principal tenant/demo: `backend/anchi_demo.db`
- Base master: `master.db`

## Ejecucion de la app

Desde `backend/`:

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Workers

Trabajadores identificados:

- `backend/app/workers/email_worker.py`
- `backend/app/workers/jobs_worker.py`

Comando estable documentado para jobs:

```bash
APP_ENV=development ./.venv/bin/python -m app.workers.jobs_worker
```

El worker usa la misma resolucion de tenant que el proceso web y recupera jobs bloqueados al arrancar.

## Observabilidad

- `LOG_FORMAT=json` deja trazas estructuradas listas para correlacionar request y jobs.
- `LOG_LEVEL=info` mantiene una salida compacta y útil para desarrollo y demo.
- `PERFORMANCE_PROFILING_ENABLED=true` activa la linea base objetiva de rendimiento en desarrollo y test.
- `GET /health/live` comprueba que el proceso esta vivo sin tocar bases.
- `GET /health/ready` valida master, tenant y métricas basicas.
- `GET /health/metrics` expone el snapshot interno de requests y jobs.
- `GET /logs` permite filtrar por `request_id`, `correlation_id`, accion y entidad.
- `backend/performance-results/` almacena las salidas generadas por el benchmark y no se versiona.

## Problemas conocidos del entorno

- El repositorio no tenia commits iniciales al empezar esta fase.
- `python` no esta disponible en el PATH del sistema; el binario correcto es el del venv.
- La app usa SQLite local y varios ficheros `.db` ya presentes.
- Hay estado untracked amplio en el repositorio de trabajo.
