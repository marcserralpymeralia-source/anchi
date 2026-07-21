# Validación final de demo de Anchi

## Objetivo

Confirmar que la demo de Anchi funciona contra una base externa real antes de desplegar en Vercel.

## Precondiciones

- `APP_ENV=demo`
- `TENANT_DB_MODE=external`
- `MASTER_DATABASE_URL` configurada
- `TENANT_DATABASE_URL` configurada
- `IMAP_SYNC_ENABLED=false`
- `EMAIL_AUTO_SYNC_ENABLED=false`
- workers desactivados en Vercel

## Orden de validación

1. Revisar variables de entorno.
2. Ejecutar migraciones master.
3. Ejecutar `provision-demo`.
4. Ejecutar migración del tenant demo.
5. Ejecutar `seed-demo`.
6. Ejecutar `health`.
7. Arrancar la app local contra la DB externa.
8. Probar login.
9. Navegar por Gestión de pedidos, Histórico, Clientes, Productos y Configuración.
10. Verificar endpoints de health.

## Comandos

```bash
./backend/.venv/bin/python scripts/demo_ops.py migrate-master
./backend/.venv/bin/python scripts/demo_ops.py provision-demo
./backend/.venv/bin/python scripts/demo_ops.py migrate-tenant --company anchi-demo
./backend/.venv/bin/python scripts/demo_ops.py seed-demo --company anchi-demo
./backend/.venv/bin/python scripts/demo_ops.py health --company anchi-demo
```

## Esperado en `health`

- `master_db: OK`
- `company: Anchi Demo OK`
- `user: admin@anchi.local OK`
- `membership: OK`
- `login: OK`
- `tenant_db: OK`
- `tenant_migrations: OK`
- `customers: >= 10`
- `products: >= 20`
- `orders: >= 10`
- `imap_auto_sync: disabled`
- `vercel_workers: disabled` en Vercel

## Endpoints a revisar

- `GET /health`
- `GET /health/master`
- `GET /health/tenant`

## Criterios de aceptación

La validación está completa cuando:

- `migrate-master` funciona contra la DB externa.
- `provision-demo` funciona contra la DB externa.
- `migrate-tenant` funciona contra la DB externa.
- `seed-demo` funciona contra la DB externa.
- `health` devuelve estado OK.
- no se crea SQLite en modo demo externo.
- el login local funciona contra la DB externa.

## Observación

Si falta `MASTER_DATABASE_URL` o `TENANT_DATABASE_URL`, la app debe fallar con un error claro en configuración. No debe caer a SQLite en demo o Vercel.
