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

- `APP_NAME`
- `APP_SLUG`
- `ENVIRONMENT`
- `APP_SECRET_KEY`
- `SESSION_COOKIE`
- `DATABASE_URL`
- `MASTER_DATABASE_URL`
- `DEFAULT_COMPANY_NAME`
- `DEFAULT_ADMIN_EMAIL`
- `DEFAULT_ADMIN_PASSWORD`
- `SEED_DEMO_DATA`

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

No se ha verificado un comando CLI separado y estable para lanzarlos fuera del proceso web.

## Problemas conocidos del entorno

- El repositorio no tenia commits iniciales al empezar esta fase.
- `python` no esta disponible en el PATH del sistema; el binario correcto es el del venv.
- La app usa SQLite local y varios ficheros `.db` ya presentes.
- Hay estado untracked amplio en el repositorio de trabajo.
