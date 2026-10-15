# Entrega de fase

## 1. Objetivo

Preparar Anchi para operar como aplicación multiempresa y multiusuario, separando el control de plataforma de los datos operativos de cada empresa y dejando un panel Superadmin independiente.

## 2. Alcance ejecutado

- Separación de identidad y control de plataforma en una base master.
- Contexto de tenant explícito por sesión y membresía.
- Migraciones idempotentes para la base master y para cada base tenant, aplicadas durante arranque/provisioning y no durante la navegación normal.
- Provisión de compañías, usuarios, membresías y bases tenant.
- Panel Superadmin para compañías, usuarios, estadísticas y auditoría.
- Las empresas son espacios de trabajo sin credenciales; los accesos se crean después como usuarios tenant.
- El Superadmin global queda reservado a la identidad de plataforma configurada y separado de las membresías tenant.
- La entrada del Superadmin a una empresa usa una sesión temporal de soporte, sin crear una membresía empresarial.
- El acceso de soporte queda registrado en la auditoría de plataforma; su actor operativo no aparece ni se puede editar en Usuarios.
- Los canales tenant reciben Email como opción visible por defecto junto a WhatsApp, aunque todavía no exista un buzón conectado.
- Selector de empresa para usuarios con varias membresías.
- Invitaciones y recuperación de contraseña con tokens de un solo uso.
- Invalidación de sesiones al cambiar contraseña, membresía o estado de usuario.
- Permisos centralizados para operaciones sensibles.
- Protección de mutaciones frente a orígenes externos no confiables.
- Aislamiento de jobs y consultas de WhatsApp por compañía.
- Índice master para resolver rápidamente endpoints de WhatsApp sin escanear todos los tenants.
- Cifrado en reposo de URLs de bases tenant.
- Namespaces por compañía para previews de importación, adjuntos, branding y exportaciones FTP.
- Comprobaciones de propiedad en descargas, confirmaciones de importación y exportaciones.
- Paginación SQL del panel Superadmin y snapshots de salud/uso por tenant.
- Protección contra la suspensión del último propietario activo de una compañía.
- Arranque local con provisión de esquemas y worker operativo.
- Sesiones server-side revocables, ligadas a usuario/membresía/empresa y rotadas al cambiar de contexto.
- Cookie de sesión opaca: el navegador solo recibe un identificador aleatorio; el contexto queda en `user_sessions`.
- Token CSRF global para formularios y `fetch`, con defensa adicional de origen y exclusiones limitadas a cron/webhooks firmados.
- Puente explícito `MasterUser` → actor local tenant para FKs, jobs, auditoría y borrados.
- El actor local es obligatorio para operar; no se usa el ID maestro como sustituto.
- Rate limiting compartido en la base master para login, invitaciones y recuperación de contraseña.
- Alta y reintento de empresas encolados para el worker de aprovisionamiento, con estados durables y fallo seguro.
- Workers con locks atómicos, round-robin por tenant y exclusión de empresas suspendidas.
- Snapshots con versión de esquema, métricas separadas por canal, retención y consulta SQL del último snapshot.
- Guía visual compartida para documentar la línea gráfica real de Anchi y sus componentes reutilizables.
- Panel Superadmin alineado con el shell de la aplicación: sidebar, cabecera, tipografía, controles e iconos SVG locales.

## 3. Alcance no ejecutado

- No se ha hecho push de los cambios de esta corrección.
- No se han tocado bases de datos de producción.
- No se ha cambiado la infraestructura externa de despliegue.
- El endpoint de snapshots está preparado para cron, pero el scheduler externo debe configurarse por despliegue.
- No se han añadido facturación, límites comerciales ni SSO.

## 4. Diagnostico previo

La aplicación tenía identidad y datos operativos mezclados en una única ruta de acceso. La autorización dependía principalmente del usuario tenant legacy, no existía un plano de control para compañías y no había una garantía uniforme de que los workers, las integraciones y los índices de WhatsApp usaran el tenant correcto.

También existía riesgo operacional en instalaciones antiguas: una URL tenant no disponible podía llegar al código de conexión como `None`, y el bootstrap local podía convertir accidentalmente el usuario de pruebas en Superadmin de plataforma. La entrada anterior del Superadmin creaba además una membresía `Solo lectura`, que no daba acceso a Configuración y mezclaba la identidad global con los usuarios de empresa. Algunas empresas existentes tampoco tenían una fila de canal Email visible en Canales.

## 5. Cambios realizados

- Añadido el modelo master con `MasterCompany`, `MasterUser`, membresías, invitaciones, reset de contraseña, auditoría, uso diario, provisión tenant e índice de endpoints WhatsApp.
- Añadido `EncryptedDatabaseURL` para almacenar cifradas las URLs de bases tenant, manteniendo compatibilidad de lectura con instalaciones legacy.
- Añadidas migraciones master hasta `2026.09.15.11` y migraciones tenant hasta `2026.09.15.2` para sesiones opacas, rate limiting, puente de identidad y snapshots de salud.
- Añadido el servicio de provisión y sincronización de actores master hacia la tabla operativa tenant.
- Añadida la resolución de usuarios y contexto tenant desde la sesión y la membresía activa.
- Añadido el panel `/superadmin` con gestión de compañías, usuarios, estadísticas, auditoría y entrada controlada a una compañía.
- Separada el alta de empresas de la creación de usuarios: provisionar una empresa ya no solicita ni crea credenciales.
- Reforzada la frontera entre identidad global y usuarios tenant: el email de plataforma queda reservado y un usuario tenant recibe `403` en `/superadmin`.
- Añadidas invitaciones y recuperación de contraseña; los tokens se almacenan hasheados, expiran y no se reutilizan.
- Añadido bloqueo temporal tras intentos fallidos, rate limiting distribuido y versionado de sesiones.
- Centralizados permisos por ruta y método HTTP.
- Añadida validación de `Origin`/`Referer` para mutaciones externas, manteniendo excepciones para webhooks y cron.
- Aplicado el `company_id` a jobs, recuperación de jobs stale, attachments y resolución de endpoints WhatsApp.
- Aplicados namespaces por compañía a previews de importación, adjuntos locales/blob, branding y rutas remotas FTP; los nombres de ficheros se sanitizan antes de exportar.
- Añadidas comprobaciones de propiedad de compañía para descargas, confirmación de importaciones, procesamiento de correo y previsualización/exportación de pedidos.
- Añadido fallback de provisión tenant cacheado para tenants creados después del arranque, limitado al arranque/provisioning; una petición no ejecuta migraciones.
- Blindado el arranque ante URLs tenant vacías o no descifrables.
- Evitado que los fixtures de test conviertan automáticamente un usuario tenant en Superadmin.
- Impedido suspender al último propietario activo de una compañía.
- Añadido aprovisionamiento asíncrono y reintento durable; la compañía solo se activa cuando el esquema y sus actores locales están listos.
- Añadido collector de snapshots de salud/uso y endpoint de cron; el panel Superadmin muestra estado, latencia y actividad diaria. Se retienen 365 días y el dashboard consulta solo el último snapshot por empresa.
- Añadida paginación SQL a compañías y usuarios del panel Superadmin y cambiado `export-preview` a POST con control de propiedad.
- Añadidos tests de aislamiento, lifecycle, Superadmin y workers.
- Añadido el shell visual del panel Superadmin sin alterar sus rutas, permisos ni acciones administrativas.
- Ajustada la compatibilidad del rate limiter con dobles ligeros usados por los tests del login, sin cambiar el comportamiento de producción.
- Reemplazada la membresía implícita de soporte por un contexto temporal ligado a la sesión server-side y limitado al tenant seleccionado; se valida de nuevo la identidad global, empresa, versión de sesión y base operativa en cada petición.
- Añadido un actor local técnico sin credenciales utilizables y fuera de las membresías para conservar referencias de auditoría tenant; se oculta y se bloquea su edición desde Usuarios.
- Las membresías históricas del dueño global se conservan sin borrarlas, pero no se muestran como usuarios empresariales ni se incluyen en el contador de accesos.
- Añadido el evento `company.support_access` a la auditoría master al entrar en una empresa.
- Añadida la migración tenant `2026.10.15.2` para crear idempotentemente el canal Email que falte, sin cambiar canales ya existentes.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/auth/dependencies.py` | Identidad master/tenant, permisos y soporte temporal | Refactor de autenticación |
| `backend/app/auth/routes.py` | Login, selector e lifecycle | Rutas nuevas y cambios de flujo |
| `backend/app/auth/sessions.py` | Sesiones revocables y rotación | Sesión server-side |
| `backend/app/auth/session_middleware.py` | Cookie opaca y persistencia de sesión | Middleware |
| `backend/app/auth/rate_limit.py` | Throttling compartido de autenticación | Seguridad |
| `backend/app/core/csrf.py` | Token CSRF y validación | Seguridad de mutaciones |
| `backend/app/core/lifespan.py` | Arranque master/tenant y workers | Provisión y tolerancia de errores |
| `backend/app/core/middleware.py` | Contexto y protección de mutaciones | Seguridad y observabilidad |
| `backend/app/core/permissions.py` | Autorización centralizada | Mapa de permisos |
| `backend/app/core/router_registry.py` | Registro del panel | Registro de rutas |
| `backend/app/db/models.py` | Puente de identidad tenant | Modelo operativo |
| `backend/app/jobs/service.py` | Aislamiento de jobs | Consultas con compañía |
| `backend/app/master/__init__.py` | Exposición del plano master | API interna |
| `backend/app/master/bootstrap.py` | Bootstrap seguro | Inicialización |
| `backend/app/master/models.py` | Entidades master | Modelos y cifrado |
| `backend/app/master/provisioning.py` | Provisión/sincronización y actor interno de soporte tenant | Servicio de dominio |
| `backend/app/master/service.py` | Contexto y autenticación | Servicio de identidad |
| `backend/app/migrations/registry.py` | Migraciones master/tenant y canal Email por defecto | Registro de versiones |
| `backend/app/settings/channels_routes.py` | Estado de canales por tenant | Aislamiento de configuración |
| `backend/app/static/styles.css` | Panel y pantallas nuevas | Estilos |
| `backend/app/templates/base.html` | Navegación Superadmin | Plantilla |
| `backend/app/templates/login.html` | Mensajes de lifecycle | Plantilla |
| `backend/app/templates/users/list.html` | Gestión de usuarios | Plantilla |
| `backend/app/tenancy/database.py` | Provisionado cacheado, errores y sesión de soporte | Resolución tenant |
| `backend/app/users/routes.py` | Gestión tenant, ocultación y protección de actores internos | Usuarios tenant |
| `backend/tests/test_multitenancy_auth.py` | Verificar acceso temporal, auditoría y ocultación de identidades globales | Tests E2E |
| `backend/tests/test_schema_migrations.py` | Verificar creación idempotente del canal Email | Tests de migración |
| `backend/app/whatsapp/inbox_routes.py` | Aislamiento WhatsApp | Rutas |
| `backend/app/whatsapp/service.py` | Índice de endpoints | Resolución master |
| `backend/app/workers/jobs_worker.py` | Worker por compañía | Aislamiento de ejecución |
| `docker-compose.yml` | Separación web/worker | Evitar doble poller de jobs |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `backend/app/auth/lifecycle.py` | Invitaciones y recuperación de contraseña |
| `backend/app/auth/rate_limit.py` | Rate limiting compartido sobre la base master |
| `backend/app/auth/session_middleware.py` | Sesiones server-side con cookie opaca |
| `backend/app/superadmin/__init__.py` | Paquete Superadmin |
| `backend/app/superadmin/routes.py` | Endpoints del panel Superadmin |
| `backend/app/superadmin/service.py` | Provisión, usuarios, estadísticas y auditoría | Servicio de dominio |
| `backend/app/workers/provisioning_worker.py` | Aprovisionamiento asíncrono de empresas | Worker |
| `backend/app/core/csrf.py` | Protección CSRF para formularios y peticiones AJAX |
| `backend/app/static/js/security.js` | Envío automático del token CSRF |
| `backend/app/superadmin/metrics.py` | Snapshots de salud y uso por tenant |
| `backend/app/cron/routes.py` | Disparo controlado del collector de snapshots |
| `backend/app/templates/forgot_password.html` | Solicitud de recuperación |
| `backend/app/templates/invitation.html` | Aceptación de invitación |
| `backend/app/templates/reset_password.html` | Cambio de contraseña |
| `backend/app/templates/select_company.html` | Selección de empresa |
| `backend/app/templates/superadmin/audit.html` | Auditoría de plataforma |
| `backend/app/templates/superadmin/companies.html` | Gestión de compañías |
| `backend/app/templates/superadmin/dashboard.html` | Resumen Superadmin |
| `backend/app/templates/superadmin/layout.html` | Layout aislado del panel |
| `backend/app/templates/superadmin/users.html` | Usuarios de compañía |
| `backend/tests/test_import_storage.py` | Namespaces de previews y adjuntos |
| `backend/tests/test_export_transport.py` | Namespace de exportaciones FTP |
| `backend/tests/test_account_lifecycle.py` | Tests de invitación y reset |
| `backend/tests/test_multitenancy_auth.py` | Tests end-to-end de sesión/tenant |
| `backend/tests/test_superadmin.py` | Tests de control de plataforma |
| `backend/tests/test_worker_tenant_scope.py` | Tests de aislamiento de workers |
| `docs/development/multiuser-multitenancy-superadmin-plan.md` | Plan técnico de fases |
| `docs/development/styles.md` | Guía de estilos visuales compartidos |

## 8. Decisiones tecnicas

| Decision | Motivo | Alternativas descartadas |
|---|---|---|
| Base master separada lógicamente del tenant | Evita que cada empresa pueda leer el control de plataforma | Mantener toda la identidad en la base operativa |
| Tenant seleccionado por membresía y sesión versionada | Permite varias empresas por usuario e invalida sesiones antiguas | Confiar solo en `company_id` enviado por formulario |
| Migraciones idempotentes en arranque/provisión | Compatibilidad con instalaciones existentes | Crear tablas manualmente desde cada ruta |
| Superadmin bajo `/superadmin` | Separa administración de plataforma de la operación diaria | Mostrar gestión global dentro del menú tenant |
| Entrada del dueño global con sesión de soporte temporal y sin membresía | Mantiene permisos de diagnóstico/configuración sin convertir al dueño en usuario de cada empresa | Crear una membresía `Solo lectura` implícita |
| Permisos centralizados por ruta/método | Reduce omisiones en nuevas acciones | Comprobar permisos de forma dispersa en cada handler |
| Índice master para WhatsApp | Evita escanear bases tenant para cada webhook | Resolver recorriendo todos los tenants |
| Cifrado de URLs tenant | No dejar credenciales/rutas sensibles legibles en master | Guardarlas como texto plano |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python -m compileall -q app tests` | OK |
| `python -m unittest -q tests.test_schema_migrations tests.test_security_config tests.test_superadmin tests.test_tenant_isolation tests.test_multitenancy_auth` | 64 tests OK |
| Smoke de `run_provisioning_cycle(max_runs=1)` con master/tenant SQLite temporales | Aprovisionamiento asíncrono completado y recursos liberados correctamente |
| Smoke del rate limiter con buckets SQLite temporales | Bloqueo y `retry_after` verificados |
| `git diff --check` | Sin errores de diff; solo avisos de conversión LF/CRLF de Git |
| Arranque Uvicorn local en `127.0.0.1:8000` | OK; `/login`, `/health` y `/static/js/security.js` devuelven 200 |
| `APP_ENV=test` + batería de login (`tests.test_templating_paths`, `tests.test_security_config` y dos casos de `tests.test_core`) | 29 tests OK |
| AX tree y captura visual de `/superadmin` tras reiniciar Uvicorn | OK; shell, navegación, tablas e iconos SVG visibles |
| `backend\.venv\Scripts\python.exe -m compileall -q app tests` (desde `backend`) | OK; sin errores sintácticos |
| `pytest tests/test_multitenancy_auth.py -q` | 6 passed; entrada temporal, acceso a configuración, auditoría y membresías históricas |
| `pytest tests/test_superadmin.py -q` | 11 passed; incluye el contador de accesos sin el dueño global |
| `pytest tests/test_schema_migrations.py -q` | 28 passed; migración Email idempotente incluida |
| `pytest tests/test_tenant_isolation.py -q` | 8 passed; aislamiento entre empresas e IDs coincidentes |
| `pytest tests/test_account_lifecycle.py -q` | 1 passed; invitaciones y revocación de sesiones |
| `pytest tests/test_worker_tenant_scope.py -q` | 1 passed; scope tenant de workers |
| `git diff --check` | OK; solo avisos de conversión LF/CRLF de Git |

Las seis suites focalizadas posteriores suman 55 tests aprobados. Se ejecutaron con SQLite temporal sintético; no se consultaron bases ni datos de producción. Persisten avisos deprecados de FastAPI/Starlette y del adaptador datetime de SQLite, sin fallos asociados.

La ejecución global `unittest discover` no se usa como criterio de esta fase porque mantiene workers de ciclo de vida y mezcla fixtures legacy. La batería relacionada con esta fase terminó correctamente con 64 tests, además de los smoke tests aislados indicados arriba.

## 10. Tests añadidos o modificados

- Tests de creación y aislamiento de compañías.
- Tests de selector de empresa y renovación/invalidez de sesiones.
- Tests de invitación y recuperación de contraseña.
- Tests de roles, permisos y panel Superadmin.
- Tests de acceso temporal de soporte, ausencia de membresía creada, auditoría y acceso a Configuración/Canales.
- Tests de que actores de soporte y membresías históricas del Superadmin no se muestran como usuarios tenant.
- Test de migración que crea el canal Email una sola vez por empresa.
- Tests de resolución WhatsApp mediante índice master.
- Tests de claim y recuperación de jobs con `company_id`.
- Tests de compatibilidad de conexión BBDD y bootstrap en entorno `test`.

## 11. Criterios de aceptacion

| Criterio | Estado | Evidencia |
|---|---|---|
| Una identidad puede tener membresías por empresa | Cumplido | `test_multitenancy_auth.py` |
| Una sesión tenant no puede cambiar de empresa manipulando parámetros | Cumplido | `load_tenant_context` y tests de sesión |
| Existe panel Superadmin separado | Cumplido | `/superadmin` y templates dedicadas |
| Se pueden crear compañías y usuarios desde plataforma | Cumplido | `superadmin/routes.py` y tests |
| Los tenants nuevos reciben esquema operativo | Cumplido | provisión y migraciones idempotentes |
| Las sesiones se invalidan tras cambios críticos | Cumplido | `session_version` y lifecycle tests |
| Los workers respetan la compañía | Cumplido | `test_worker_tenant_scope.py` |
| WhatsApp resuelve el tenant sin escaneo completo | Cumplido | índice `whatsapp_endpoints` y test |
| Previews, adjuntos, branding y exportaciones tienen namespace de compañía | Cumplido | servicios de almacenamiento/exportación y tests |
| El panel Superadmin pagina compañías y usuarios en SQL | Cumplido | `superadmin/routes.py` y plantillas |
| El panel Superadmin muestra salud/uso recientes | Cumplido | snapshots, collector, cron y dashboard |
| No se puede suspender al último propietario activo | Cumplido | `test_superadmin.py` |
| El Superadmin entra con contexto temporal y no se convierte en miembro de la empresa | Cumplido | `test_platform_owner_entering_company_can_open_configuration_and_email_channel` |
| Configuración muestra Email y WhatsApp aunque Email aún no exista en el tenant | Cumplido | `test_default_email_channel_migration_seeds_empty_companies_idempotently` |
| El acceso de soporte se audita y no aparece como usuario empresarial | Cumplido | `test_multitenancy_auth.py` y `PlatformAuditLog` |
| Las altas de empresas no bloquean la petición web | Cumplido | `provisioning_worker.py` y estados `pending/running/failed` |
| La autenticación y lifecycle tienen rate limiting compartido | Cumplido | `MasterRateLimitBucket` y `auth/rate_limit.py` |
| La app arranca localmente con el estado actual | Cumplido | Uvicorn + `/login` 200 |
| El panel Superadmin comparte el lenguaje visual de Anchi sin perder sus acciones | Cumplido | tests relacionados, AX tree y captura visual local |

## 12. Riesgos y observaciones pendientes

- Antes de producción hay que ejecutar una migración controlada de master y verificar el `ENCRYPTION_KEY` estable; cambiarlo sin procedimiento de rotación dejaría URLs cifradas ilegibles.
- Las instalaciones existentes deben migrar/revisar objetos legacy que aún no estén bajo namespace; las nuevas escrituras ya quedan aisladas por compañía.
- La exportación FTP usa `tenant-{company_id}` como directorio remoto; hay que verificar que cada servidor FTP permita `MKD` y aplicar una migración operativa si el destino actual esperaba la raíz.
- El collector de snapshots debe programarse y monitorizarse en cada despliegue.
- El envío de invitaciones en producción debe conectarse a un proveedor de correo transaccional y monitorizarse.
- El panel Superadmin mantiene una navegación propia y separada del menú operativo tenant.
- Deben revisarse rutas legacy que aún puedan asumir un único usuario tenant.
- Las membresías de soporte creadas por versiones anteriores no se borran automáticamente; se excluyen de listados y contadores. Su eliminación requiere una limpieza de datos aprobada y separada.
- En producción el worker de jobs se desactiva en el proceso web por configuración (`RUN_INTERNAL_JOB_WORKER=false`) y se mantiene en el servicio dedicado; el worker de email puede mantenerse en el web tradicional o desactivarse explícitamente.

## 13. Desviaciones respecto al alcance inicial

Se añadió el índice de endpoints WhatsApp, el cifrado de URLs tenant, el aislamiento de almacenamiento, los snapshots de salud, la cookie opaca, el rate limiting y el aprovisionamiento asíncrono porque el análisis mostró que eran necesarios para evitar fugas de contexto y bloqueos operativos. Facturación, SSO y límites comerciales quedan fuera de esta fase.

## 14. Estado final de Git

- Rama actual: `chore/technical-improvement-plan`.
- Hay cambios locales sin commit.
- No se ha hecho commit ni push.
- No se han descartado cambios locales ajenos.
- La corrección de soporte temporal y canal Email queda registrada en un commit local separado, sin push.
- Los cambios locales ajenos de WhatsApp se conservan fuera de ese commit.
- Las validaciones de esta corrección usaron `TestClient` y bases SQLite temporales; no se consultaron datos de producción ni se reinició un servidor web.

## 15. Recomendacion para la siguiente fase

Crear una migración de despliegue separada y validarla primero contra una copia de staging: comprobar cifrado de URLs, crear una empresa de prueba desde Superadmin, aceptar su invitación, cambiar de empresa, verificar aislamiento de pedidos/correo/WhatsApp y probar recuperación tras reinicio de workers. Después completar la revisión de rutas legacy, el proveedor transaccional y el scheduler externo antes de publicar el flujo multiempresa.
