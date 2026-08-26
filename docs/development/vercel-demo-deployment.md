# Demo funcional de Anchi en Vercel

## Resumen

Esta preparación deja Anchi listo para una demo estable en Vercel con entrada FastAPI, empaquetado ligero y un flujo claro de bootstrap de base de datos, compañía demo y datos de ejemplo.

La recomendación práctica es:

- usar Vercel para la demo web y la API visible;
- usar una base de datos externa persistente para `master` y `tenant`;
- mantener workers y procesos pesados fuera del runtime de Vercel;
- desactivar IMAP automático en demo salvo activación manual.

Nota importante: el flujo `seed-base` sigue existiendo para desarrollo local, pero la demo pública debe ir por `provision-demo` + DB externa.

## Cambios aplicados en el repositorio

- `index.py` como entrypoint Vercel.
- `requirements.txt` en la raíz para instalar dependencias del backend.
- `vercel.json` con configuración mínima de función.
- `.vercelignore` para excluir tests, scripts y artefactos locales.
- La política pública de privacidad está disponible en `/privacy`; para esta demo, la URL de Meta es `https://anchi-tan.vercel.app/privacy`.
- `scripts/demo_ops.py` con comandos separados de migración, provisioning, seed y health.
- `backend/app/core/lifespan.py` ya evita arrancar workers en Vercel.
- `backend/app/core/config.py` y `backend/app/demo_seed.py` ya usan la contraseña demo acordada.

## Entry point de Vercel

Vercel detecta FastAPI si encuentra una instancia `app` en un entrypoint soportado. En este repositorio ese punto de entrada es `index.py`, que añade `backend/` al `PYTHONPATH` y expone `app.main:app`.

## Comandos de bootstrap

Usa estos comandos desde la raíz del proyecto:

```bash
python scripts/demo_ops.py migrate-master
python scripts/demo_ops.py provision-demo
python scripts/demo_ops.py migrate-tenant --company anchi-demo
python scripts/demo_ops.py seed-demo --company anchi-demo
python scripts/demo_ops.py health --company anchi-demo
```

Si necesitas aplicar el esquema a todos los tenants activos:

```bash
python scripts/demo_ops.py migrate-all-tenants
```

## Variables de entorno

Configura como mínimo:

- `APP_ENV=demo`
- `APP_URL`
- `MASTER_DATABASE_URL`
- `TENANT_DB_MODE=external`
- `TENANT_DATABASE_URL`
- `DATABASE_URL` como alias opcional del tenant si quieres mantener compatibilidad
- `AUTH_SECRET`
- `JWT_SECRET`
- `SESSION_SECRET`
- `TENANT_DB_ENCRYPTION_KEY`
- `CRON_SECRET`
- `OPENAI_API_KEY`
- `OPENAI_DEFAULT_MODEL`
- `STORAGE_PROVIDER`
- `STORAGE_BUCKET`
- `STORAGE_ACCESS_KEY`
- `STORAGE_SECRET_KEY`
- `IMAP_SYNC_ENABLED=false`
- `EMAIL_AUTO_SYNC_ENABLED=false`
- `LOG_LEVEL=info`
- `DEFAULT_ADMIN_EMAIL=admin@anchi.local`
- `DEFAULT_ADMIN_PASSWORD=AnchiDemo2026!`

Regla clave:

- `APP_ENV=demo` o `VERCEL=1` no deben arrancar con SQLite por defecto.
- si `TENANT_DB_MODE=external`, `TENANT_DATABASE_URL` es obligatorio.
- si `MASTER_DATABASE_URL` o `TENANT_DATABASE_URL` apuntan a SQLite en demo/Vercel, la app debe fallar con un error claro.

Semántica recomendada:

- `MASTER_DATABASE_URL` identifica la base maestra.
- `TENANT_DATABASE_URL` identifica la base operativa demo.
- `DATABASE_URL` sigue siendo compatible como alias interno del tenant, pero en demo externa conviene usar `TENANT_DATABASE_URL` como variable canónica.

## Health checks

Verifica estos endpoints tras desplegar:

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /health/master`
- `GET /health/tenant`

Respuesta esperada como mínimo:

```json
{
  "status": "ok",
  "environment": "demo",
  "master_db": "ok",
  "tenant_db": "ok"
}
```

## Flujo recomendado de despliegue

1. Crea o conecta las bases de datos externas para `master` y `tenant`.
2. Configura las variables de entorno de demo en Vercel.
3. Ejecuta `migrate-master`.
4. Ejecuta `provision-demo`.
5. Ejecuta `migrate-tenant`.
6. Ejecuta `seed-demo`.
7. Despliega en Vercel.
8. Comprueba login y navegación por Gestión de pedidos, Histórico, Clientes, Productos y Configuración.

Si todavía no has conectado Postgres externo, `seed-base` sigue siendo útil para desarrollo local, pero no para la demo pública en Vercel.

## Limitaciones importantes

- Los workers de email y jobs no deben correr como procesos infinitos en Vercel.
- IMAP automático debe quedar desactivado en demo salvo activación manual.
- SQLite local sirve para desarrollo, pero no para una demo persistente en Vercel.
- Si quieres guardar adjuntos, correcciones o históricos reales, usa almacenamiento y base de datos externos.

## Arquitectura recomendada si quieres robustez real

Si la demo crece o quieres evitar cualquier fragilidad de serverless, la arquitectura más sólida sigue siendo:

- frontend y capa pública en Vercel;
- backend FastAPI y workers en Render, Railway o Fly.io;
- base de datos en Neon, Supabase o Railway;
- almacenamiento de adjuntos en S3 compatible.

Así mantienes la demo en Vercel, pero quitas del runtime lo que más riesgo introduce.
