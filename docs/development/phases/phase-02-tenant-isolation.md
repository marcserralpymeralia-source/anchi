# Fase 2 - Aislamiento multi-tenant y autorizacion

## 1. Objetivo

Garantizar que ningun usuario, request o servicio pueda acceder a datos de otra compania y que las rutas master queden separadas del runtime tenant.

## 2. Alcance ejecutado

- Resolucion estricta del tenant con `membership_id`, `user_id` y `company_id` coherentes.
- Rechazo de sesiones incompletas o manipuladas.
- Eliminacion de la resolucion de tenant por `company_id` no validado.
- Separacion de acceso master reservando `Superadmin`.
- Tests de dos companias con IDs coincidentes.
- Documentacion de las reglas de aislamiento.

## 3. Alcance no ejecutado

- No se tocaron jobs, workers ni rendimiento.
- No se reorganizo la arquitectura de carpetas.
- No se redisenaron permisos funcionales.
- No se migraron datos entre bases.

## 4. Diagnostico previo

| Area | Estado encontrado | Riesgo | Clasificacion |
|---|---|---|---|
| Resolucion de tenant | `get_tenant_db` podia abrir por `company_id` de sesion sin validar membresia completa | Acceso cruzado por sesion manipulada | Vulnerabilidad confirmada |
| Sesion tenant | `load_tenant_context` aceptaba combinaciones parciales y resolvia por slug/host | Tenant incorrecto por contexto incompleto | Riesgo preventivo |
| Acceso master | `require_master_admin` aceptaba tambien `Administrador` | Un admin de compania podia entrar en soporte/plataforma | Vulnerabilidad confirmada |
| Cobertura | No habia prueba especifica de dos companias con IDs coincidentes | Regresion silenciosa | Falta de cobertura |

## 5. Cambios realizados

- Se obligo a que el tenant se resuelva solo con una membresia valida y consistente.
- Se hizo que `get_tenant_db` dependa del contexto ya validado.
- Se endurecio el acceso master a `Superadmin`.
- Se añadieron tests para sesiones incompletas, slug y host manipulados, membresias cruzadas y IDs coincidentes en distintas DB.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/master/service.py` | Endurecer resolucion de tenant | Logica de autorizacion |
| `backend/app/tenancy/database.py` | Evitar apertura de DB por identificador no validado | Logica de acceso |
| `backend/app/auth/dependencies.py` | Separar acceso master de acceso tenant | Autorizacion |
| `backend/tests/test_tenant_isolation.py` | Cubrir aislamiento entre companias | Tests nuevos |
| `docs/development/acceptance-criteria.md` | Registrar contrato de aislamiento | Documentacion |
| `docs/development/decision-log.md` | Registrar decisiones de seguridad | Documentacion |
| `docs/development/change-log.md` | Registrar cambios de fase | Documentacion |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/development/tenant-isolation.md` | Regla operativa y checklist del aislamiento |
| `docs/development/phases/phase-02-tenant-isolation.md` | Informe de entrega de la fase |
| `backend/tests/test_tenant_isolation.py` | Demostracion automatica del aislamiento |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| Exigir `membership_id + user_id + company_id` | Evita resoluciones ambiguas | Resolver por slug o company_id solo |
| Validar slug y host contra la membresia | Evita secuestro de contexto por URL | Confiar en host o slug como fuente de verdad |
| Reservar `Superadmin` para master | Separa plataforma de compania sin redisenar roles | Mantener `Administrador` como acceso master |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `APP_ENV=test ./.venv/bin/python -m unittest tests.test_tenant_isolation` | OK |
| `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests` | OK |
| `APP_ENV=development ./.venv/bin/python -m compileall app` | OK |
| `APP_ENV=development ./.venv/bin/python -c \"from app.main import app; print(app.title)\"` | OK |

## 10. Tests añadidos o modificados

- `backend/tests/test_tenant_isolation.py`
- Ajustes menores en tests de seguridad para mantener el contexto de arranque estable.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| Fuente de verdad clara para el tenant | Cumplido | `load_tenant_context` valida membresia, usuario y compania |
| Cada ruta tenant requiere contexto valido | Cumplido | `get_tenant_db` usa solo tenant validado |
| Tenant DB resuelta desde Master DB validada | Cumplido | `MasterTenantDatabase` + membresia |
| Entrada manipulada no elige base | Cumplido | tests de slug, host y company_id |
| Membresia de otro usuario se rechaza | Cumplido | tests de cross-user session |
| Membresia inactiva se rechaza | Cumplido | tests dedicados |
| Compañia inactiva se rechaza | Cumplido | tests dedicados |
| IDs coincidentes no producen acceso cruzado | Cumplido | dos tenant DB con customer_id = 1 |
| Rutas master requieren permiso master | Cumplido | `require_master_admin` exige `Superadmin` |
| Suite completa pasa | Cumplido | `unittest discover -s tests` |

## 12. Riesgos y observaciones pendientes

- El sistema aun conserva rutas legacy que no se revisaron en profundidad fuera del alcance.
- El cambio a `Superadmin` como acceso master puede requerir revisar cuentas demo o de soporte existentes.

## 13. Desviaciones respecto al alcance inicial

- No se introdujo un framework de permisos nuevo.
- No se tocaron migraciones.
- No se cambiaron modelos operativos.

## 14. Estado final de Git

`6a5b81e` - `security: enforce tenant isolation`

## 15. Recomendacion para la siguiente fase

Cerrar la provision de tenants y el diagnostico operativo de plataforma, ahora con la separacion master/tenant ya reforzada.
