# Fase 4 - Observabilidad y trazabilidad operativa

## 1. Objetivo

Correlacionar requests, jobs, logs y diagnosticos con un contexto comun, manteniendo el sistema ligero y sin dependencias nuevas.

## 2. Alcance ejecutado

- Contexto de observabilidad con `request_id`, `correlation_id`, tenant y usuario.
- Logging estructurado con redaccion de datos sensibles.
- Persistencia de trazas en auditoria con contexto legible por maquina.
- Propagacion de trazas a jobs y worker asíncrono.
- Endpoints `health/live`, `health/ready` y `health/metrics`.
- Snapshot interno de metricas de requests y jobs.
- Filtros de logs por request, correlation y entidad.
- Diagnostico admin con un resumen de observabilidad.
- Tests de observabilidad.

## 3. Alcance no ejecutado

- No se introdujo OpenTelemetry, Prometheus ni un colector externo.
- No se cambio el modelo de datos maestro/tenant.
- No se rediseñaron dashboards completos de operacion.

## 4. Cambios principales

| Archivo | Cambio |
|---|---|
| `backend/app/core/observability.py` | Contexto comun, redaccion y trazas estructuradas |
| `backend/app/core/logging.py` | Formato JSON/TEXT con contexto actual |
| `backend/app/core/metrics.py` | Snapshot interno de requests y jobs |
| `backend/app/core/middleware.py` | Request/correlation ids y duracion por request |
| `backend/app/logs/service.py` | Auditoria estructurada y parser de trazas |
| `backend/app/jobs/service.py` | Propagacion de trazas en jobs |
| `backend/app/workers/jobs_worker.py` | Contexto por job y logging de inicio/fin |
| `backend/app/health/routes.py` | Live, ready y metrics |
| `backend/app/admin/routes.py` | Resumen de observabilidad en diagnostico |
| `backend/app/templates/*` | Superficie visual de trazas y diagnosticos |
| `backend/tests/test_observability.py` | Cobertura de trazas, health y metrics |

## 5. Validaciones

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_observability`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=development ./.venv/bin/python -m compileall app`

## 6. Riesgos y observaciones

- Los logs estructurados son mas utiles para maquinas, pero cambian la lectura manual de consola.
- Las metricas son internas y en memoria; al reiniciar el proceso se reinician tambien.
- La persistencia de trazas en `AuditLog.message` depende de parsear JSON en la UI, asi que la compatibilidad visual debe cuidarse.

## 7. Recomendacion siguiente

La siguiente mejora natural es ampliar la exploracion de trazas en la UI operativa y llevar parte de esta observabilidad a la bandeja y al detalle de pedido.

