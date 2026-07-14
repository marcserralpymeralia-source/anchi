| Fecha | Fase | Archivo | Cambio | Validacion |
|---|---|---|---|---|
| 2026-07-14 | Fase 0 | `AGENTS.md` | Se añadio una instruccion minima de alcance y validacion | `git status`, revision manual |
| 2026-07-14 | Fase 0 | `docs/development/environment.md` | Guia del entorno de desarrollo | Revisado manualmente |
| 2026-07-14 | Fase 0 | `docs/development/validation-commands.md` | Tabla de comandos oficiales | Validada contra repo y pruebas |
| 2026-07-14 | Fase 0 | `docs/development/acceptance-criteria.md` | Criterios globales de aceptacion | Revisado manualmente |
| 2026-07-14 | Fase 0 | `docs/development/phase-delivery-template.md` | Plantilla de entrega por fase | Revisado manualmente |
| 2026-07-14 | Fase 0 | `docs/development/decision-log.md` | Registro de decisiones | Revisado manualmente |
| 2026-07-14 | Fase 0 | `docs/development/change-log.md` | Registro de cambios | Revisado manualmente |
| 2026-07-14 | Fase 0 | `docs/development/phases/phase-00-preparation.md` | Informe de fase 0 | Revisado manualmente |
| 2026-07-14 | Fase 0.5 | `.gitignore` | Se ampliaron exclusiones para caches, entornos, bases, adjuntos y temporales | `git status --ignored`, `git check-ignore` |
| 2026-07-14 | Fase 0.5 | `docs/development/decision-log.md` | Se registro la decision de crear la linea base Git | Revision manual |
| 2026-07-14 | Fase 0.5 | `docs/development/change-log.md` | Se registro el cierre de la linea base | Revision manual |
| 2026-07-14 | Fase 1 | `backend/app/core/config.py` | Se centralizaron las validaciones de entorno, cookies, hosts, CORS y secretos | `unittest`, arranque local y rechazo de config insegura |
| 2026-07-14 | Fase 1 | `backend/app/core/app_factory.py` | Se aplicaron cookies y middlewares segun entorno | `unittest`, import de la app |
| 2026-07-14 | Fase 1 | `backend/app/core/encryption.py` | Se separo la clave de cifrado y se redacciono la mascara de secretos | `unittest` |
| 2026-07-14 | Fase 1 | `backend/app/templates/login.html` | Se eliminaron credenciales demo precargadas | Revision manual |
| 2026-07-14 | Fase 1 | `backend/app/admin/diagnostics.py` | Se retiraron datos de identificacion de la base tenant en diagnosticos | Revision manual |
| 2026-07-14 | Fase 1 | `backend/app/health/routes.py` | Se retiro la clave de la base tenant de la respuesta de salud | Revision manual |
| 2026-07-14 | Fase 1 | `backend/.env.example` | Se documentaron las variables de seguridad y despliegue seguras | Revision manual |
| 2026-07-14 | Fase 1 | `docs/development/*` | Se actualizo la guia de entorno, validaciones y criterios de aceptacion | Revision manual |
| 2026-07-14 | Fase 2 | `backend/app/master/service.py` | Se endurecio la resolucion del tenant para exigir sesion y membresia coherentes | `unittest`, acceso cruzado bloqueado |
| 2026-07-14 | Fase 2 | `backend/app/tenancy/database.py` | Se elimino la resolucion de base por `company_id` no validado | `unittest`, rechazo de sesiones invalidas |
| 2026-07-14 | Fase 2 | `backend/app/auth/dependencies.py` | Se separo el acceso master reservandolo a `Superadmin` | `unittest` |
| 2026-07-14 | Fase 2 | `backend/tests/test_tenant_isolation.py` | Se añadieron pruebas de aislamiento multi-compania e IDs coincidentes | `unittest` |
| 2026-07-14 | Fase 2 | `docs/development/*` | Se documentaron las reglas de aislamiento y el cierre de fase | Revision manual |
