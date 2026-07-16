# Deployment Runbook

## Arranque local

```bash
cd backend
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Staging con Docker

```bash
docker compose up --build
```

Servicios:

- `web`
- `worker`
- `postgres`

## Comprobaciones

- `GET /health/live`
- `GET /health/ready`
- login
- home
- channels
- orders
- jobs
- webhook WhatsApp

## Backup y restauracion

La base objetivo es PostgreSQL. En un entorno con `pg_dump` y `pg_restore` se deben guardar backups por timestamp, restaurar en una base temporal y validar:

- master,
- tenant,
- jobs,
- mensajes,
- pedidos.

## Rollback

No se usa downgrade ficticio. El rollback operativo consiste en:

1. parar web y worker,
2. restaurar backup,
3. volver a arrancar,
4. comprobar health/readiness,
5. reabrir el servicio.

