| Fecha | Fase | Decision | Motivo | Archivos afectados |
|---|---|---|---|---|
| 2026-07-14 | Fase 0 | Crear base documental de entorno, validacion y entregas | Dejar una referencia repetible para las siguientes fases | `AGENTS.md`, `docs/development/*` |
| 2026-07-14 | Fase 0 | Crear rama `chore/technical-improvement-plan` | Identificar la secuencia de mejoras sin tocar funcionalidad | Ninguno |
| 2026-07-14 | Fase 0.5 | Crear la linea base Git inicial con exclusiones locales estrictas | Obtener un punto de comparacion limpio sin secretos, bases ni entorno virtual | `.gitignore`, codigo fuente, documentacion de desarrollo |
| 2026-07-14 | Fase 1 | Centralizar la seguridad de configuracion en `app.core.config` | Impedir que produccion arranque con secretos, demo bootstrap o cookies inseguras | `backend/app/core/config.py`, `backend/app/core/app_factory.py`, `backend/app/core/encryption.py` |
| 2026-07-14 | Fase 1 | Exigir configuracion explicita de hosts, CORS y cookies por entorno | Evitar defaults abiertos y hacer el despliegue mas predecible | `backend/app/core/config.py`, `backend/app/core/app_factory.py`, `backend/.env.example` |
| 2026-07-14 | Fase 1 | Retirar credenciales demo del login | Evitar que la pantalla de acceso exponga valores iniciales reutilizables | `backend/app/templates/login.html` |
| 2026-07-14 | Fase 1 | Redactar secretos en salida textual | Evitar fugas en placeholders, repr y diagnosticos | `backend/app/core/encryption.py`, `backend/app/admin/diagnostics.py`, `backend/app/health/routes.py` |
| 2026-07-14 | Fase 2 | Exigir `membership_id`, `user_id` y `company_id` sincronizados para resolver tenant | Evitar que slug, host o company_id por si solos abran una base incorrecta | `backend/app/master/service.py`, `backend/app/tenancy/database.py` |
| 2026-07-14 | Fase 2 | Reservar `Superadmin` para rutas master | Separar administracion de plataforma de la administracion de compania | `backend/app/auth/dependencies.py`, `backend/app/admin/routes.py`, `backend/app/health/routes.py` |
| 2026-07-14 | Fase 2 | Introducir tests de aislamiento entre dos companias | Demostrar que IDs coincidentes no producen acceso cruzado | `backend/tests/test_tenant_isolation.py` |
| 2026-07-14 | Fase 3 | Consolidar idempotencia de jobs por clave estable y restriccion unica | Evitar duplicados activos y repetidos para la misma operacion | `backend/app/jobs/service.py`, `backend/app/db/models.py` |
| 2026-07-14 | Fase 3 | Registrar intentos de job y recovery de abandonados | Hacer trazable cada adquisicion y cada reintento | `backend/app/db/models.py`, `backend/app/jobs/service.py`, `backend/app/workers/jobs_worker.py` |
| 2026-07-14 | Fase 3 | Añadir entrada CLI estable para el worker de jobs | Poder ejecutar y probar la cola sin depender del arranque web | `backend/app/workers/jobs_worker.py`, `docs/development/environment.md` |
