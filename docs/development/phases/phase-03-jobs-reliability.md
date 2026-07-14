# Fase 3 - Fiabilidad de jobs y procesos asincronos

## 1. Objetivo

Hacer que los jobs existentes sean idempotentes, trazables y recuperables sin cambiar la arquitectura de colas ni introducir infraestructura nueva.

## 2. Alcance ejecutado

- Inventario y consolidacion de estados de job.
- Idempotencia por clave estable.
- Restriccion unica para evitar duplicados equivalentes.
- Historial de intentos con `JobAttempt`.
- Recovery de jobs bloqueados.
- Limite de intentos y backoff configurable.
- Worker con identidad estable y entrada CLI.
- Guardas de idempotencia en procesado de correo e importacion.
- Tests de jobs fiables y worker controlado.
- Documentacion de entorno, criterios y comando del worker.

## 3. Alcance no ejecutado

- No se sustituyo la cola existente.
- No se introdujo Redis, RabbitMQ, Celery ni otra infraestructura distribuida.
- No se rediseño el monitor visual.
- No se toco el pipeline de IA mas alla de impedir duplicaciones.
- No se implemento backfill IMAP.

## 4. Diagnostico previo

| Area | Estado encontrado | Riesgo | Clasificacion |
|---|---|---|---|
| Idempotencia | `enqueue_job` reutilizaba solo jobs activos | Duplicados tras estados terminales | Fallo reproducido |
| Concurrencia | La adquisicion dependia de select/update sin historial | Dos workers podian competir sin trazabilidad | Riesgo preventivo |
| Reintentos | No habia historia de intentos por job | Dificil auditar abandonos y fallos | Inconsistencia |
| Recovery | No existia recuperacion explicita de jobs stale | Jobs bloqueados indefinidamente | Fallo reproducido |
| Correo | `process_email` podia reentrar sobre la misma entrada | Pedidos duplicados en retries | Fallo reproducido |
| Importacion | No habia guardas de reentrada para el mismo archivo | Duplicacion de importaciones | Riesgo preventivo |

## 5. Cambios realizados

- Se añadió `JobAttempt` para registrar cada adquisicion.
- Se reforzo `BackgroundJob` con restriccion unica por `company_id`, `job_type` y `dedupe_key`.
- Se centralizaron duraciones, reintentos, recovery y finalizacion en `app.jobs.service`.
- Se añadió `JOB_MAX_ATTEMPTS`, backoff configurable y `JOB_STALE_AFTER_SECONDS`.
- Se recuperan jobs `running` abandonados al arrancar el worker.
- Se añadió `run_worker_cycle()` y `python -m app.workers.jobs_worker`.
- Se bloqueo la reentrada de `process_email` cuando la entrada ya tiene pedido/no-pedido/dudoso resuelto.
- Se bloqueo la reimportacion repetida del mismo archivo completado.
- Se expuso el historial de intentos en el detalle del job.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/core/config.py` | Configuracion de jobs | Configuracion |
| `backend/app/db/models.py` | `JobAttempt` y restriccion unica | Modelo |
| `backend/app/jobs/service.py` | Idempotencia, claims, retries, recovery | Logica de servicio |
| `backend/app/jobs/routes.py` | Exponer `max_attempts` e intentos | API/serializacion |
| `backend/app/templates/jobs/detail.html` | Mostrar historial de intentos | Plantilla |
| `backend/app/templates/jobs/monitor.html` | Mostrar maximo de intentos | Plantilla |
| `backend/app/workers/jobs_worker.py` | Recovery, worker identity, CLI estable | Worker |
| `backend/app/agent/services.py` | Evitar reprocesar la misma entrada | Logica de dominio |
| `backend/.env.example` | Variables de jobs | Documentacion de entorno |
| `docs/development/environment.md` | Comando del worker y variables | Documentacion |
| `docs/development/validation-commands.md` | Validaciones de jobs | Documentacion |
| `docs/development/acceptance-criteria.md` | Criterios de jobs fiables | Documentacion |
| `docs/development/decision-log.md` | Decisiones de fase 3 | Registro |
| `docs/development/change-log.md` | Resumen de cambios de fase 3 | Registro |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/tests/test_jobs_reliability.py` | Suite especifica de jobs fiables |
| `docs/development/jobs-reliability.md` | Guia operativa de jobs fiables |
| `docs/development/phases/phase-03-jobs-reliability.md` | Memoria de la fase |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| Mantener el sistema de jobs actual | Minimizar el cambio y el riesgo | Introducir una cola externa |
| Guardar `JobAttempt` | Tener trazabilidad real por ejecucion | Depender solo de logs sueltos |
| Reutilizar la clave de idempotencia por payload canónico | Evitar duplicados con poca complejidad | Versionado complejo por cada job |
| Recuperar jobs stale al arrancar | Evitar bloqueos perpetuos | Heartbeat continuo |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `APP_ENV=test ./.venv/bin/python -m unittest tests.test_jobs_reliability` | OK |
| `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests` | OK |
| `APP_ENV=development ./.venv/bin/python -m compileall app` | OK |
| `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"` | OK |

## 10. Tests añadidos o modificados

- Idempotencia de `enqueue_job`.
- Historial de intentos y finalizacion.
- Recuperacion de jobs stale.
- Idempotencia de `process_email`.
- Idempotencia de `import_confirm`.
- Ejecucion del worker en un ciclo controlado.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| Jobs equivalentes no se duplican | Cumplido | `enqueue_job` reutiliza la misma clave |
| Un job no lo ejecutan dos workers a la vez | Cumplido | `claim_next_job` con bloqueo y test especifico |
| Existe historial de intentos | Cumplido | Tabla `job_attempts` |
| Hay limite de intentos | Cumplido | `JOB_MAX_ATTEMPTS` + `max_retries` almacenado |
| Hay backoff | Cumplido | Reintento determinista con `JOB_RETRY_BASE_SECONDS` |
| Jobs abandonados se recuperan | Cumplido | `recover_stale_jobs()` y test |
| El worker tiene comando estable | Cumplido | `python -m app.workers.jobs_worker` |
| El retry no duplica pedidos ni importaciones | Cumplido | Tests de correo e importacion |

## 12. Riesgos y observaciones pendientes

- El flujo de exportacion por FTP sigue siendo una zona delicada si un proveedor externo aceptara la operacion y el proceso muriera antes del commit final.
- La restriccion unica de jobs depende de la base activa; en entornos existentes con tablas antiguas conviene revisar la creacion de indices al arrancar.
- El monitor aun no muestra el detalle completo de intentos en la lista principal, solo en la vista detalle.

## 13. Desviaciones respecto al alcance inicial

- Se añadio una guardia extra en el procesado de correo para cortar duplicaciones reales detectadas durante la fase.
- Se añadio una guardia extra en importaciones para evitar repetir un archivo ya completado.

## 14. Estado final de Git

- Rama: `chore/technical-improvement-plan`
- Estado: con cambios pendientes de commit

## 15. Recomendacion para la siguiente fase

Seguir con la carga diferida y la UX operativa, dejando ya cerrada la base de jobs fiables y la separacion master/tenant.
