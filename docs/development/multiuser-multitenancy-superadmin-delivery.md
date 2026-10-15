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

La revisión adicional encontró que el alta aceptaba reutilizar una misma base operativa en varias empresas. Eso contradecía el aislamiento por base definido para esta fase y podía hacer que accesos legacy por ID resolvieran registros de otra compañía.

## 5. Cambios realizados

- Añadido el modelo master con `MasterCompany`, `MasterUser`, membresías, invitaciones, reset de contraseña, auditoría, uso diario, provisión tenant e índice de endpoints WhatsApp.
- Añadido `EncryptedDatabaseURL` para almacenar cifradas las URLs de bases tenant, manteniendo compatibilidad de lectura con instalaciones legacy.
- Rechazada la asignación de un destino de base de datos ya vinculado a otra empresa; se compara el endpoint sin credenciales y se normalizan los hosts direct/pooler de Neon.
- Añadidas migraciones master y tenant para sesiones opacas, rate limiting, puente de identidad, snapshots de salud, control del bot por conversación y canal Email predeterminado.
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
- Corregido el branding inicial de tenants nuevos: el nombre comercial parte del nombre de su propia empresa, sin sobrescribir filas de identidad ya guardadas.
- Ajustada la compatibilidad del rate limiter con dobles ligeros usados por los tests del login, sin cambiar el comportamiento de producción.
- Reemplazada la membresía implícita de soporte por un contexto temporal ligado a la sesión server-side y limitado al tenant seleccionado; se valida de nuevo la identidad global, empresa, versión de sesión y base operativa en cada petición.
- Añadido un actor local técnico sin credenciales utilizables y fuera de las membresías para conservar referencias de auditoría tenant; se oculta y se bloquea su edición desde Usuarios.
- Las membresías históricas del dueño global se conservan sin borrarlas, pero no se muestran como usuarios empresariales ni se incluyen en el contador de accesos.
- Añadido el evento `company.support_access` a la auditoría master al entrar en una empresa.
- Corregido el cierre de sesiones tenant inválidas: al revocar o cambiar la versión de sesión, se limpia el contexto y se redirige al login en lugar de dejar un 403 persistente.
- Añadida la migración tenant `2026.10.15.2` para crear idempotentemente el canal Email que falte, sin cambiar canales ya existentes.
- Evitada una inspección duplicada de `schema_migrations` en el diagnóstico de esquema tenant; se reutiliza una única lectura de columnas.
- Corregida una fuga de estado entre suites: los tests de extracción ya no fuerzan `APP_ENV=development` al importar el módulo cuando CI ya seleccionó `test`.
- Centralizada la identidad de URLs de bases tenant y aplicada también a la provisión externa, validando antes de modificar la empresa o abrir una conexión externa.
- Serializada la asignación de bases en PostgreSQL con `pg_advisory_xact_lock` por identidad normalizada; los hosts directos y pooler de Neon esperan el mismo lock transaccional.
- Estabilizada la comprobación de fecha del pedido para comparar la fecha local de `Europe/Madrid`, igual que la plantilla, en vez de comparar el valor UTC sin convertir.

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
| `backend/app/settings/branding.py` | Usar el nombre de la empresa tenant al crear su identidad inicial | Corrección de default tenant |
| `backend/app/static/styles.css` | Panel y pantallas nuevas | Estilos |
| `backend/app/templates/base.html` | Navegación Superadmin | Plantilla |
| `backend/app/templates/login.html` | Mensajes de lifecycle | Plantilla |
| `backend/app/templates/users/list.html` | Gestión de usuarios | Plantilla |
| `backend/app/tenancy/database.py` | Provisionado cacheado, errores y sesión de soporte | Resolución tenant |
| `backend/app/tenancy/migrations.py` | Reutilizar inspección de columnas del reporte de migración | Optimización de consulta |
| `backend/app/users/routes.py` | Gestión tenant, ocultación y protección de actores internos | Usuarios tenant |
| `backend/app/superadmin/service.py` | Rechazar destinos de base tenant ya asignados a otra empresa | Validación de aprovisionamiento |
| `backend/tests/test_multitenancy_auth.py` | Verificar acceso temporal, auditoría y ocultación de identidades globales | Tests E2E |
| `backend/tests/test_schema_migrations.py` | Verificar creación idempotente del canal Email | Tests de migración |
| `backend/tests/test_superadmin.py` | Verificar unicidad de destino de base de datos por empresa | Tests de aprovisionamiento |
| `backend/tests/test_order_extraction_service.py` | Respetar el entorno `test` configurado por el runner | Aislamiento de configuración de tests |
| `backend/tests/test_orders_detail_optimization.py` | Comparar la fecha mostrada usando la zona horaria tenant | Estabilidad de test |
| `backend/tests/test_setup_onboarding.py` | Verificar que la identidad inicial usa el nombre de su empresa tenant | Regresión de onboarding |
| `docs/development/multiuser-multitenancy-superadmin-delivery.md` | Registrar la corrección de branding y sus validaciones locales | Evidencia de fase |
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
| `backend/app/superadmin/service.py` | Provisión, usuarios, estadísticas, auditoría y validación de unicidad de base tenant | Servicio de dominio |
| `backend/app/master/database_identity.py` | Identidad común de URL y guard compartido de provisión | Módulo nuevo |
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
| `pytest tests/test_multitenancy_auth.py -q` | 7 passed; entrada temporal, acceso a configuración, auditoría, membresías históricas y revocación |
| `pytest tests/test_superadmin.py -q` | 11 passed; incluye el contador de accesos sin el dueño global |
| `pytest tests/test_schema_migrations.py -q` | 28 passed; migración Email idempotente incluida |
| `pytest tests/test_tenant_isolation.py -q` | 8 passed; aislamiento entre empresas e IDs coincidentes |
| `pytest tests/test_account_lifecycle.py -q` | 1 passed; invitaciones y revocación de sesiones |
| `pytest tests/test_worker_tenant_scope.py -q` | 1 passed; scope tenant de workers |
| Primera suite global, antes del arreglo de `APP_ENV` | 588 tests, 10 fallos; se reprodujeron siete fallos de multitenancy por el valor global de entorno impuesto al importar el módulo de extracción |
| Suite global tras aislar `APP_ENV`, antes del ajuste de migraciones | 593 tests, un fallo reproducible: detalle de pedido medio con 16 consultas (límite 15) |
| `APP_ENV=test; RUN_INTERNAL_JOB_WORKER=false; RUN_INTERNAL_EMAIL_WORKER=false; .venv\Scripts\python.exe -m unittest discover -s tests -v` (desde `backend`) | 599 tests OK; 5 omitidos; 1382.980 s. Pasada global final tras todos los cambios; solo SQLite de pruebas |
| `APP_ENV=test; RUN_INTERNAL_JOB_WORKER=false; RUN_INTERNAL_EMAIL_WORKER=false; .venv\Scripts\python.exe -m unittest tests.test_superadmin tests.test_multitenancy_auth tests.test_schema_migrations tests.test_orders_detail_optimization -v` (desde `backend`) | 55 tests OK; 151.770 s |
| `APP_ENV=test; RUN_INTERNAL_JOB_WORKER=false; RUN_INTERNAL_EMAIL_WORKER=false; .venv\Scripts\python.exe -m unittest tests.test_superadmin tests.test_schema_migrations -v` (desde `backend`) | 43 tests OK; 57.863 s, tras el guard previo a mutaciones |
| `APP_ENV=test; RUN_INTERNAL_JOB_WORKER=false; RUN_INTERNAL_EMAIL_WORKER=false; .venv\Scripts\python.exe -m unittest tests.test_superadmin -v` (desde `backend`) | 15 tests OK; 44.631 s, incluido el lock transaccional por URL Neon |
| `APP_ENV=test` y workers por defecto; `python -m unittest tests.test_order_extraction_service tests.test_multitenancy_auth tests.test_orders_detail_optimization tests.test_orders_list_optimization -v` (desde `backend`) | 25 tests OK; 77.331 s. Reproduce los defaults del job SQLite de CI |
| `APP_ENV=test` y workers por defecto; test aislado `test_order_date_defaults_to_email_date_and_saves_iso` | 1 test OK; confirma la expectativa de fecha local |
| `APP_ENV=test; .venv\Scripts\python.exe -m compileall -q app scripts tests` (desde `backend`) | OK; sin errores sintácticos |
| `APP_ENV=test RUN_INTERNAL_JOB_WORKER=false RUN_INTERNAL_EMAIL_WORKER=false .\.venv\Scripts\python.exe -m compileall -q app\settings\branding.py tests\test_setup_onboarding.py` (desde `backend`) | OK; sintaxis del cambio de branding |
| `APP_ENV=test RUN_INTERNAL_JOB_WORKER=false RUN_INTERNAL_EMAIL_WORKER=false .\.venv\Scripts\python.exe -m unittest tests.test_setup_onboarding tests.test_branding_cache -v` (desde `backend`) | 18 tests OK en 79.164 s; aparecieron avisos `ResourceWarning` al cerrar la suite, sin fallo de test |
| `$env:APP_ENV='test'; $env:RUN_INTERNAL_JOB_WORKER='false'; $env:RUN_INTERNAL_EMAIL_WORKER='false'; .\.venv\Scripts\python.exe -m unittest tests.test_multitenancy_auth tests.test_superadmin tests.test_schema_migrations tests.test_tenant_isolation -v` (PowerShell, desde `backend`) | 59 tests OK en 134.837 s; incluye soporte del dueño, acceso tenant a Canales, bloqueo de `/superadmin`, Email en migraciones y aislamiento entre tenants. Al salir apareció un `ResourceWarning` de una conexión SQLite |
| `$env:APP_ENV='test'; $env:RUN_INTERNAL_JOB_WORKER='false'; $env:RUN_INTERNAL_EMAIL_WORKER='false'; .\.venv\Scripts\python.exe -m unittest tests.test_multitenancy_auth.MultiTenancyAuthenticationTests.test_selecting_membership_uses_the_selected_tenant_database -v` (PowerShell, desde `backend`) | 1 test OK en 9.554 s; administrador tenant abre Configuración, Canales y el drawer de Email, ve ambos canales y no ve ni puede abrir Superadmin |
| `$env:APP_ENV='test'; $env:RUN_INTERNAL_JOB_WORKER='false'; $env:RUN_INTERNAL_EMAIL_WORKER='false'; .\.venv\Scripts\python.exe -m unittest tests.test_multitenancy_auth.MultiTenancyAuthenticationTests.test_single_company_admin_can_open_email_settings_without_platform_access -v` (PowerShell, desde `backend`) | 1 test OK en 8.138 s; login directo del único tenant asignado, panel de Email disponible y Superadmin inaccesible |
| Navegación local aislada con Playwright CLI en `http://127.0.0.1:8010` | Con datos sintéticos: el dueño global entró en una empresa recién creada, abrió Configuración y Canales y vio Email y WhatsApp; la cabecera identificó la empresa correcta. No se consultó producción |
| `git diff --check` | OK; sin errores de whitespace; Git avisa de conversión LF/CRLF en Windows |

Las suites se ejecutaron con SQLite temporal sintético; no se consultaron bases ni datos de producción. Persisten avisos deprecados de FastAPI/Starlette y del adaptador datetime de SQLite, sin fallos asociados.

La auditoría adicional aprobó `python -m unittest tests.test_superadmin -q` (13 tests), `tests.test_tenant_isolation` (8), `tests.test_multitenancy_auth` (7) y `tests.test_schema_migrations` (29). La última incluye una actualización desde `2026.10.15.1` que aplica `2026.10.15.2` y crea el canal Email que faltaba.

Los fallos globales previos quedaron diagnosticados y corregidos: contaminación de `APP_ENV` por un test de extracción y una inspección SQL redundante en el reporte de migraciones. La suite global final de 599 tests pasó con workers internos desactivados; también pasaron las suites focalizadas (25 con los valores por defecto de CI, 43 de Superadmin/migraciones y 15 de Superadmin tras añadir el lock). La pasada global se ejecutó después de todos los cambios de código y del test de zona horaria.

## 10. Tests añadidos o modificados

- Tests de creación y aislamiento de compañías.
- Tests de selector de empresa y renovación/invalidez de sesiones.
- Tests de invitación y recuperación de contraseña.
- Tests de roles, permisos y panel Superadmin.
- Tests de acceso temporal de soporte, ausencia de membresía creada, auditoría y acceso a Configuración/Canales.
- Tests de que actores de soporte y membresías históricas del Superadmin no se muestran como usuarios tenant.
- Test de que revocar la sesión global invalida y cierra inmediatamente el acceso temporal a empresa.
- Test de migración que crea el canal Email una sola vez por empresa.
- Test de actualización desde la versión tenant anterior para verificar el canal Email en instalaciones existentes.
- Tests que impiden asignar el mismo destino de base de datos a varias empresas aunque cambien credenciales o se use el host pooler de Neon.
- Tests de resolución WhatsApp mediante índice master.
- Tests de claim y recuperación de jobs con `company_id`.
- Tests de compatibilidad de conexión BBDD y bootstrap en entorno `test`.
- Test de que importar los tests de extracción no sobrescribe `APP_ENV=test`.
- Test de que la provisión externa valida la base antes de abrir la conexión o modificar la empresa.
- Test de que URLs directas y pooler equivalentes de Neon comparten la misma clave de advisory lock PostgreSQL.
- Test que comprueba la fecha del pedido en la zona tenant y no depende de que el reloj ejecute la prueba antes o después de medianoche UTC.
- Test de que una fila de identidad nueva toma el nombre de la compañía tenant y conserva el nombre de aplicación Anchi.
- Ampliado el test de selección de membresía para recorrer como administrador tenant `/settings` y `/settings/channels`, comprobar los dos canales y verificar que el acceso a `/superadmin` sigue denegado.
- Añadido el recorrido de login de una membresía única, que reproduce el acceso directo del administrador empresarial sin selector de compañía y verifica el drawer de Email y la separación del Superadmin.

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
| Una base operativa no se comparte accidentalmente al crear otra empresa | Cumplido | `test_company_creation_rejects_a_database_already_assigned_to_another_company` |
| El acceso de soporte se audita y no aparece como usuario empresarial | Cumplido | `test_multitenancy_auth.py` y `PlatformAuditLog` |
| Las altas de empresas no bloquean la petición web | Cumplido | `provisioning_worker.py` y estados `pending/running/failed` |
| La autenticación y lifecycle tienen rate limiting compartido | Cumplido | `MasterRateLimitBucket` y `auth/rate_limit.py` |
| La app arranca localmente con el estado actual | Cumplido | Uvicorn + `/login` 200 |
| El panel Superadmin comparte el lenguaje visual de Anchi sin perder sus acciones | Cumplido | tests relacionados, AX tree y captura visual local |
| Un tenant nuevo recibe como nombre comercial inicial el nombre de su propia empresa | Cumplido | `test_new_tenant_branding_defaults_to_its_company_name` y navegación local en una empresa sintética recién provisionada |
| Un administrador tenant puede abrir el panel de Email y ver Email/WhatsApp sin acceder al Superadmin de plataforma | Cumplido | `test_selecting_membership_uses_the_selected_tenant_database` y `test_single_company_admin_can_open_email_settings_without_platform_access`; ambos flujos devuelven 200 en ajustes/canales/email, 403 en `/superadmin` y no muestran el enlace global |

## 12. Riesgos y observaciones pendientes

- Antes de producción hay que ejecutar una migración controlada de master y verificar el `ENCRYPTION_KEY` estable; cambiarlo sin procedimiento de rotación dejaría URLs cifradas ilegibles.
- Las instalaciones existentes deben migrar/revisar objetos legacy que aún no estén bajo namespace; las nuevas escrituras ya quedan aisladas por compañía.
- La exportación FTP usa `tenant-{company_id}` como directorio remoto; hay que verificar que cada servidor FTP permita `MKD` y aplicar una migración operativa si el destino actual esperaba la raíz.
- El collector de snapshots debe programarse y monitorizarse en cada despliegue.
- El envío de invitaciones en producción debe conectarse a un proveedor de correo transaccional y monitorizarse.
- El panel Superadmin mantiene una navegación propia y separada del menú operativo tenant.
- Deben revisarse rutas legacy que aún puedan asumir un único usuario tenant.
- La protección evita asignaciones duplicadas nuevas; no se consultaron datos reales, por lo que una revisión aprobada debe verificar si ya existen URLs asignadas a varias empresas.
- En PostgreSQL las altas concurrentes de la misma base se serializan con advisory lock transaccional. Otros dialectos distintos de PostgreSQL no tienen ese bloqueo; si se usan con varias réplicas, habría que añadir un mecanismo equivalente.
- Las membresías de soporte creadas por versiones anteriores no se borran automáticamente; se excluyen de listados y contadores. Su eliminación requiere una limpieza de datos aprobada y separada.
- En producción el worker de jobs se desactiva en el proceso web por configuración (`RUN_INTERNAL_JOB_WORKER=false`) y se mantiene en el servicio dedicado; el worker de email puede mantenerse en el web tradicional o desactivarse explícitamente.
- Las filas de branding ya existentes no se reescriben automáticamente: pueden contener una identidad personalizada y deben revisarse aparte si muestran un nombre anterior. La navegación local también registró un 404 de `/favicon.ico` y sugerencias de `autocomplete` en login; son observaciones fuera del cambio de branding.

## 13. Desviaciones respecto al alcance inicial

Se añadió el índice de endpoints WhatsApp, el cifrado de URLs tenant, el aislamiento de almacenamiento, los snapshots de salud, la cookie opaca, el rate limiting y el aprovisionamiento asíncrono porque el análisis mostró que eran necesarios para evitar fugas de contexto y bloqueos operativos. Facturación, SSO y límites comerciales quedan fuera de esta fase.

## 14. Estado final de Git

- Rama actual: `chore/technical-improvement-plan`.
- Las correcciones de soporte temporal, canal Email, revocación de sesión, propiedad de base tenant y validaciones globales están registradas en commits locales separados.
- Permanecen 14 archivos locales previos de WhatsApp/canales/workers sin incluir; se conservaron intactos y fuera de esta corrección.
- Commits locales de esta continuación: `7b25404 test: avoid leaking app environment between suites`, `96781ff perf: avoid duplicate tenant schema inspection`, `0342936 fix: guard external tenant database isolation`, `a3dbf7c test: compare order date in tenant timezone` y `1ad0c2f fix: serialize tenant database claims`.
- También permanecen los commits previos de aislamiento `7990970 fix: prevent sharing tenant databases` y `3fd6b68 docs: record tenant database isolation guard`.
- El arreglo de branding está en `a8c6b90`, la auditoría en `69e7594`, el acceso de membresía múltiple en `59ecadc` y el drawer de Email en `4209d59`; el login directo de tenant único y esta evidencia se registran juntas en este commit (20 commits por delante de `origin/chore/technical-improvement-plan`). No se hizo push ni despliegue.
- La rama permanece únicamente local; no se ha hecho push ni desplegado.
- No se han descartado cambios locales ajenos.
- Los cambios locales no publicados aún no han pasado CI remoto ni Vercel.
- Las validaciones automatizadas usaron bases SQLite temporales y `TestClient`; no se conectó directamente a una base de datos productiva.

## 15. Recomendacion para la siguiente fase

Crear una migración de despliegue separada y validarla primero contra una copia de staging: comprobar cifrado de URLs, crear una empresa de prueba desde Superadmin, aceptar su invitación, cambiar de empresa, verificar aislamiento de pedidos/correo/WhatsApp y probar recuperación tras reinicio de workers. Después completar la revisión de rutas legacy, el proveedor transaccional y el scheduler externo antes de publicar el flujo multiempresa.
