# Observabilidad operativa

## Objetivo

Hacer que cada request, job y accion operativa pueda seguirse con un contexto comun sin introducir infraestructura externa.

## Piezas principales

- `request_id`: identifica una peticion concreta.
- `correlation_id`: une la peticion web con los jobs y acciones derivadas.
- `tenant_id` y `tenant_slug`: contextualizan la compania activa.
- `user_id` y `membership_id`: enlazan con la sesion valida.
- `job_id` y `worker_id`: enlazan ejecuciones asíncronas y worker.

## Dónde se expone

- Middleware web: asigna request/correlation y registra duracion.
- Logs de auditoria: guardan contexto estructurado dentro de `AuditLog.message`.
- Jobs: embeben la traza original en el payload y la recuperan en el worker.
- Health: expone `/health/live`, `/health/ready` y `/health/metrics`.
- Diagnostico admin: resume metrics internas y el estado de cada tenant.

## Reglas

- No guardar secretos en la traza.
- No mezclar trazas entre tenants.
- Mantener el contexto en formato legible por maquina.
- Conservar compatibilidad con los endpoints y paginas existentes.

## Comandos utiles

- `curl http://127.0.0.1:8000/health/live`
- `curl http://127.0.0.1:8000/health/ready`
- `curl http://127.0.0.1:8000/health/metrics`
- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_observability`

