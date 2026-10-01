# Checklist del entorno de piloto

Estado inicial de esta auditoría: **NO GO** hasta completar y adjuntar evidencia en cada casilla obligatoria.

## Datos del entorno

- [ ] Entorno identificado: `pilot/staging`, región, URL, commit y fecha de despliegue.
- [ ] Tenant(s) de prueba y responsable operativo identificados.
- [ ] No se usan datos de producción para pruebas de aceptación.
- [ ] Se ha definido el volumen esperado: pedidos/hora, adjuntos/día, usuarios concurrentes, ventana de soporte.

## Configuración obligatoria

- [ ] `APP_ENV=production` en el entorno real; `DEBUG` desactivado.
- [ ] `SECRET_KEY` fuerte y distinto de cualquier valor demo.
- [ ] `ENCRYPTION_KEY` Fernet válida y almacenada en secret manager.
- [ ] `DATABASE_URL` y `MASTER_DATABASE_URL` apuntan a PostgreSQL gestionado; no SQLite.
- [ ] `TENANT_DB_MODE` y `TENANT_DATABASE_URL` son coherentes con el modelo de tenants elegido.
- [ ] `SESSION_COOKIE_SECURE=true`, `SESSION_COOKIE_SAMESITE` y dominio revisados.
- [ ] `ALLOWED_HOSTS` explícito; `CORS_ALLOWED_ORIGINS` sin wildcard con credenciales.
- [ ] `ENABLE_DEMO_BOOTSTRAP=false`; credenciales de administrador iniciales cambiadas y rotadas.
- [ ] `CRON_SECRET` definido y diferente entre ambientes.
- [ ] Web: `RUN_INTERNAL_JOB_WORKER=false` cuando exista worker dedicado.
- [ ] Worker: exactamente un despliegue dedicado, con acceso a las mismas DB y secrets.
- [ ] `TEMP_STORAGE_DIR` apunta a un volumen persistente, o Vercel Blob privado está configurado con `BLOB_READ_WRITE_TOKEN`.
- [ ] Permisos de lectura/escritura, cuota, retención y cifrado del storage verificados.
- [ ] `LOG_FORMAT=json`, `LOG_LEVEL` y redacción de secretos revisados.

## Base de datos y migraciones

- [ ] Backup/snapshot previo con timestamp, propietario y retención.
- [ ] `report-master` y `status-all-tenants --summary` guardados.
- [ ] `upgrade-master --dry-run` y `upgrade-all-tenants --dry-run` sin cambios no previstos.
- [ ] Simulación sobre copia completada; segunda ejecución es no-op.
- [ ] Upgrade real ejecutado tenant por tenant o con procedimiento aprobado.
- [ ] Versión/checksum de master y cada tenant registrados.
- [ ] Readiness confirma master, tenant y migraciones actuales.
- [ ] Restore en staging validado para master, tenant, jobs, mensajes, pedidos y adjuntos.

Comandos de referencia desde `backend/`:

```bash
PYTHONPATH=. python -m scripts.schema_migrations report-master
PYTHONPATH=. python -m scripts.schema_migrations status-all-tenants --summary
PYTHONPATH=. python -m scripts.schema_migrations upgrade-master --dry-run
PYTHONPATH=. python -m scripts.schema_migrations upgrade-all-tenants --dry-run
PYTHONPATH=. python -m scripts.schema_migrations simulate-all-tenants --application-version <version>
```

## Canales y conectores del primer piloto

Contrato recomendado para el primer piloto:

- **Entrada**: WhatsApp Catalog estructurado, con catálogo y clientes previamente mapeados.
- **Destino**: un único Generic REST o ERP concreto con healthcheck, timeout e idempotency key.
- **Fuera de alcance**: email autodiscovery, email backfill con adjuntos, conversación libre con LLM, audio/transcripción, múltiples destinos simultáneos, billing/marketplace.

- [ ] Meta webhook verify token y app secrets configurados sin aparecer en logs.
- [ ] `phone_number_id`, business account y catálogo son los del tenant correcto.
- [ ] Se prueba firma/identidad, duplicado, cliente desconocido y producto desconocido.
- [ ] El destino responde healthcheck y documenta códigos retryable/permanent.
- [ ] Se prueba timeout antes de crear, éxito remoto con timeout local y error permanente.
- [ ] Se verifica que un pedido produce una sola outbox, un solo job y una sola entrega externa.

## Workers, cron y observabilidad

- [ ] `/health/live`, `/health/ready`, `/health/master` y `/health/tenant` capturados.
- [ ] Heartbeat del worker y edad máxima de backlog tienen alerta.
- [ ] Cron del proveedor ejecuta `/cron/jobs` y snapshots; email sólo si está en alcance.
- [ ] Un 401/403 del cron genera alerta y no se confunde con una ejecución correcta.
- [ ] Se conserva el recorrido synthetic: inbound ID → order ID → delivery ID → job ID → attempt ID → audit/correlation ID.
- [ ] Logs y audit no contienen passwords, access tokens, cookies ni payloads sensibles innecesarios.

## Seguridad, PII y soporte

- [ ] Matriz RBAC aprobada para operador, supervisor, administrador y superadmin.
- [ ] Pruebas de acceso cruzado entre dos tenants ejecutadas.
- [ ] Política de retención de mensajes, teléfonos, emails, payloads, adjuntos, logs y backups aprobada.
- [ ] Offboarding probado: desactivar usuario/canales, revocar credenciales, gestionar blobs y documentar backups.
- [ ] SLA de soporte, horario, severidad y canal de escalado definidos.
- [ ] Manual de intervención no permite editar directamente DB sin aprobación y auditoría.

## Configuraciones peligrosas: bloquear

- [ ] No `DATABASE_URL`/`MASTER_DATABASE_URL` SQLite en producción.
- [ ] No `DEBUG=true`, `ALLOWED_HOSTS=*`, CORS wildcard con cookies ni secretos demo.
- [ ] No web y worker escribiendo en DB/storage diferentes.
- [ ] No dos workers activos sin coordinación de locks.
- [ ] No Vercel sin Blob privado si se reciben adjuntos.
- [ ] No aceptar tráfico si restore no ha sido probado.
- [ ] No incluir audio/email/autodiscovery en el contrato si siguen abiertos G-04, G-05 o G-06.
