# Anchi

Aplicacion web en Python/FastAPI para gestionar pedidos recibidos por correo mediante un agente de IA. La base esta preparada para replicarse por cliente: marca, empresa, credenciales iniciales, base de datos y comportamiento demo se controlan desde `.env` y desde Configuracion.

## Arranque local

```bash
cd backend
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Abre `http://127.0.0.1:8000`.

Usuario inicial: definido en `.env` con `DEFAULT_ADMIN_EMAIL` y `DEFAULT_ADMIN_PASSWORD`.

## Replicar para un cliente

Lee [ARCHITECTURE.md](ARCHITECTURE.md), [ESCALAR_CLIENTES.md](ESCALAR_CLIENTES.md) y [CLIENTE_NUEVO.md](CLIENTE_NUEVO.md).

Resumen:

1. Genera la configuracion con `python scripts/new_client.py ...`.
2. Copia `clients/<cliente>.env` a `backend/.env`.
3. Mantén `SEED_DEMO_DATA=false` para una instalacion limpia.
4. Arranca la app y personaliza desde Configuracion.
5. Importa clientes y productos reales.

## Configuracion sensible

En produccion define siempre una clave propia:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Usa el resultado en `APP_SECRET_KEY`.

SQLite se usa por defecto para desarrollo local. Para produccion puedes usar PostgreSQL con:

```bash
DATABASE_URL="postgresql+psycopg://user:password@host:5432/db"
```
