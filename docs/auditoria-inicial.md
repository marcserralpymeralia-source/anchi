# Auditoría inicial de la aplicación

## 1. Resumen ejecutivo

La aplicación ya no es un prototipo pequeño: es una base FastAPI bastante extensa, con multitenencia real, login contra master DB, base operativa por compañía, bandeja de pedidos, correo IMAP, scoring, learning, jobs, alertas y pantallas de administración.

El punto fuerte es que el núcleo de pedidos y correo sí existe y está conectado de extremo a extremo en buena parte del flujo. El punto débil es que la app arrastra bastante código de transición: mezcla de capas master/tenant, mucha lógica de composición en servicios grandes, varias rutas que todavía cargan más de lo que la UI necesita y varios módulos que están preparados pero no completados del todo.

No he encontrado un módulo real de proyectos/tareas/calendario ni de imputación horaria. Tampoco hay WhatsApp, voz o redes sociales implementados, solo la abstracción inicial. Eso confirma que la evolución actual está centrada en pedidos/correo y en la plataforma multi-tenant, no en la agenda operativa genérica.

## 2. Stack tecnológico

| Tecnología | Versión | Finalidad | Observaciones |
|---|---:|---|---|
| Python | 3.13 | Runtime principal | Detectado por la venv local |
| FastAPI | 0.115.6 | API y SSR con Jinja | Base de rutas y formularios |
| SQLAlchemy | 2.0.36 | ORM y acceso a datos | Usa modelos por tenant y master |
| Uvicorn | 0.34.0 | Servidor ASGI | Arranque local funcional |
| Jinja2 | 3.1.5 | Plantillas HTML | UI server-side |
| Passlib | 1.7.4 | Hash de contraseñas | Login master |
| Cryptography | 44.0.0 | Cifrado de secretos | Secretos IMAP/SMTP/LLM |
| Pandas | 2.2.3 | Importación y tablas | Import/preview |
| Openpyxl | 3.1.5 | Excel | Importaciones |
| psycopg | 3.2.3 | PostgreSQL | Soporte previsto |
| SQLite | N/A | Desarrollo y demos | Base por defecto en local |

No hay `package.json`, ni frontend SPA separado, ni Dockerfile visible en el barrido inicial.

## 3. Estructura del proyecto

El proyecto está organizado como una aplicación FastAPI monolítica modular:

- `backend/app/main.py`: arranque mínimo.
- `backend/app/core/`: config, middleware, seguridad, layout de routers, plantillas.
- `backend/app/master/`: base master, modelos de compañías/usuarios/membresías, provisionamiento.
- `backend/app/tenancy/`: resolución de tenant y sesión de base tenant.
- `backend/app/db/`: modelos operativos del tenant.
- `backend/app/auth/`: login/logout y dependencias de usuario.
- `backend/app/orders/`: edición y procesamiento de pedidos.
- `backend/app/channels/` y `backend/app/mail/`: bandeja y correo.
- `backend/app/customers/` y `backend/app/products/`: maestros de cliente y producto.
- `backend/app/imports/` y `backend/app/databases/`: importación y gestión de datos maestros.
- `backend/app/settings/`: configuración del agente, correo, branding, canales e integraciones.
- `backend/app/jobs/`: cola, visor y reintentos.
- `backend/app/learning/`: correcciones, aliases, RAG y documentos.
- `backend/app/alerts/`, `backend/app/logs/`, `backend/app/admin/`, `backend/app/health/`: operación, trazabilidad y diagnóstico.
- `backend/app/templates/` y `backend/app/static/`: UI SSR y estilos.
- `backend/tests/`: suite mínima de validación.

Hay además archivos de contexto y documentación:

- `README.md`
- `ARCHITECTURE.md`
- `ESCALAR_CLIENTES.md`
- `CLIENTE_NUEVO.md`

## 4. Arquitectura actual

### Frontend

No existe frontend separado con React/Vue/etc. La UI es SSR con Jinja, CSS propio y JavaScript mínimo para interacciones puntuales.

Fortalezas:
- estructura simple de servir;
- menos complejidad de despliegue;
- más facilidad para iterar pantallas operativas.

Debilidades:
- plantillas grandes y densas;
- parte de la lógica operativa se mezcla con presentación;
- el detalle de pedidos y la bandeja han necesitado modularización manual;
- UX todavía muy de operador experto.

### Backend

La arquitectura real ya es multi-capa:

- `app_factory` crea la app.
- `router_registry` ensambla routers.
- middleware resuelve branding y tenant.
- login se hace contra master DB.
- la mayoría de operación se hace sobre tenant DB.
- jobs desacoplan parte del trabajo pesado.

Hay una buena separación entre master y tenant, pero aún hay “puentes” y rutas legacy durante la transición.

### Base de datos

Hay dos niveles:

1. `master.db`
   - compañías
   - usuarios
   - membresías
   - tenant databases
   - estado de sincronización IMAP

2. base tenant por compañía
   - clientes
   - productos
   - pedidos
   - correos
   - adjuntos
   - alertas
   - jobs
   - aprendizaje
   - exportaciones

Esto es ya una multitenencia real, no solo un campo `company_id` cosmético.

### Infraestructura

- desarrollo local con SQLite;
- soporte para PostgreSQL vía `DATABASE_URL`;
- sesión por cookie;
- middleware de branding;
- workers de correo y jobs;
- health endpoints;
- no he visto Docker/CI/CD en este barrido.

## 5. Flujo general de la aplicación

1. El usuario entra en `/login`.
2. El login valida contra master DB.
3. Se guarda `user_id`, `company_id`, `membership_id` y `company_slug` en sesión.
4. `branding_middleware` resuelve tenant y branding.
5. `get_tenant_db` abre la base de datos de la compañía.
6. Las rutas operativas trabajan sobre el tenant.
7. IMAP descarga correos al tenant.
8. El pipeline de agente normaliza, clasifica, extrae, matchea y puntúa.
9. El usuario revisa en `workbench` / pedidos.
10. Los cambios se guardan, se aprende y se registra auditoría.
11. Jobs gestionan el trabajo pesado y reintentos.

## 6. Inventario funcional

| Módulo | Funcionalidad | Estado | Frontend | Backend | Base de datos | Problema principal | Prioridad |
|---|---|---|---|---|---|---|---|
| Auth | Login/logout | Operativa | `/login` | `auth/routes.py`, `auth/dependencies.py` | master | Credenciales demo por defecto | Alta |
| Tenancy | Resolución de tenant | Operativa | Implícito | `tenancy/database.py`, `master/service.py` | master + tenant | Capa puente aún compleja | Alta |
| Empresas | Companies/memberships | Operativa | Admin | `master/models.py`, `master/bootstrap.py` | master | Provisioning en evolución | Alta |
| Bandeja | Workbench/home | Operativa con limitaciones | `/` | `pages/routes.py`, `workbench/routes.py`, `dashboard/service.py` | tenant | Sigue siendo densa y pesada | Alta |
| Histórico | Pedidos/correos históricos | Operativa | `/pedidos`, `/history` | `pages/routes.py` | tenant | Muchos filtros y composición en Python | Media |
| Correo | IMAP sync y preview | Operativa con limitaciones | `settings/email`, `channels`, `mail` | `settings/integrations.py` | tenant | Backfill/limpieza pendiente | Alta |
| Canales | Abstracción email/whatsapp/voz/social | Parcialmente implementada | `settings/channels` | `agent/platform.py`, `channels/routes.py` | tenant | Solo email funcional | Alta |
| Pedidos | CRUD y revisión | Operativa | `/orders` | `orders/routes.py` | tenant | Ruta larga y muy cargada | Alta |
| Clientes | Maestro de clientes | Operativa | `/customers`, `/databases` | `customers/routes.py`, `databases/routes.py` | tenant | Doble vía funcional que confunde | Media |
| Productos | Maestro de productos | Operativa | `/products`, `/databases` | `products/routes.py`, `databases/routes.py` | tenant | Doble vía funcional que confunde | Media |
| Importación | Clientes/productos/preview | Operativa con limitaciones | `/imports`, `/databases` | `imports/routes.py`, `imports/service.py` | tenant | Carga y flujo UI aún mejorable | Media |
| Exportación | CSV/FTP/SFTP | Operativa con limitaciones | Settings / pedidos | `exports/service.py`, `orders/routes.py` | tenant | Depende de configuración completa | Media |
| Jobs | Cola, reintentos, monitor | Operativa | `/jobs/monitor` | `jobs/routes.py`, `jobs/service.py` | tenant | Observabilidad mejorable | Alta |
| Alertas | Centro de alertas | Operativa | Drawer + `/alerts` | `alerts/routes.py` | tenant | Polling y carga global permanente | Media |
| Learning | Correcciones, aliases, RAG | Operativa con limitaciones | `/learning` | `learning/routes.py`, `agent/platform.py` | tenant | Muchas piezas aún son soporte | Alta |
| Configuración | Email/AI/scoring/branding | Operativa | `/settings` | `settings/routes.py` | tenant | Pantalla muy densa | Alta |
| Admin | Diagnóstico y tenants | Operativa | `/admin` | `admin/routes.py`, `admin/diagnostics.py` | master + tenant | Vista técnica útil pero separada del operador | Media |
| Logs | Auditoría | Operativa | `/logs` | `logs/routes.py` | tenant | No centralizada a nivel producto | Media |
| Health | Health checks | Operativa | `/health` | `health/routes.py` | master + tenant | Solo diagnóstico, no observabilidad completa | Media |
| Proyectos/tareas/calendario | Gestión operativa genérica | No encontrada | No existe | No existe | No existe | No hay módulo real | Alta |
| Imputación de horas | Timesheets | No encontrada | No existe | No existe | No existe | No hay módulo real | Alta |

## 7. Estado de proyectos, tareas y calendario

No existe un módulo real de proyectos, tareas, subtareas, planificación diaria, calendario o imputación horaria. En el código solo aparecen referencias genéricas en documentación y algunas búsquedas de texto, pero no una capa funcional completa.

Conclusión:
- no hay modelo de datos específico;
- no hay endpoints reales;
- no hay calendario de usuario;
- no hay vista diaria/semanal/mensual;
- no hay lógica de solapamientos ni capacidad.

Esto no es una debilidad oculta: es una ausencia de producto, así que esta línea queda como fase futura.

## 8. Estado de la imputación de horas

No existe un módulo de time tracking ni registros de imputación por usuario/tarea/proyecto. Por tanto:

- no hay control de horas previstas vs consumidas;
- no hay validación de capacidad;
- no hay históricos;
- no hay interfaz de registro rápido.

## 9. Autenticación, permisos y aislamiento

### Lo que funciona

- login contra master DB;
- sesión con `membership_id`, `user_id`, `company_id`, `company_slug`;
- dependencias `current_user` / `current_tenant_user`;
- `require_tenant_role`, `require_master_role`, `require_company_membership`;
- muchas consultas filtran por `company_id`.

### Riesgos

- `DEV_SECRET_KEY` y credenciales demo en `core/config.py` y `.env.example` son seguros para desarrollo, pero peligrosos si se despliegan sin cambio.
- No se ve rate limiting ni protección anti brute-force.
- La autorización depende bastante de disciplina por endpoint; hay que vigilar rutas nuevas.
- El aislamiento por compañía es bastante bueno en la parte principal, pero la superficie es grande y hay que revisar cada nueva consulta.

## 10. Integraciones externas

| Integración | Finalidad | Estado real | Archivos clave | Riesgo |
|---|---|---|---|---|
| IMAP | Leer correos | Operativa | `settings/integrations.py`, `settings/routes.py` | Alto si la config es errónea |
| SMTP | Enviar pruebas/respuestas | Operativa con limitaciones | `settings/integrations.py`, `settings/routes.py` | Medio |
| OpenAI/LLM | Clasificación/extracción | Operativa con limitaciones | `settings/integrations.py`, `agent/services.py`, `agent/platform.py` | Alto por dependencia externa |
| FTP/SFTP | Exportación | Operativa con limitaciones | `exports/service.py`, `settings/routes.py`, `orders/routes.py` | Alto si falta conexión |
| WhatsApp | Prevista | No implementada | `agent/platform.py` | No aplicable aún |
| Voz | Prevista | No implementada | `agent/platform.py` | No aplicable aún |
| Redes sociales | Prevista | No implementada | `agent/platform.py` | No aplicable aún |

## 11. Inteligencia artificial y agentes

Hay bastante infraestructura:

- `app/agent/platform.py`: pipeline común, matching, scoring, RAG, learning.
- `app/agent/services.py`: procesamiento de email, matching, scoring, mock y hooks.
- `app/settings/integrations.py`: llamadas al proveedor LLM.
- `app/settings/agent_config.py`: métricas y estado del agente.

Estado real:
- la IA sí está integrada en el flujo;
- parte de la clasificación/extracción está soportada por prompts y servicios;
- hay modos mock y fallbacks;
- learning y RAG están orientados a apoyar, no a sustituir validación determinista.

Riesgos:
- dependencia fuerte del LLM;
- riesgo de respuestas no válidas;
- parte del pipeline aún depende de servicios auxiliares y datos de prueba;
- algunos estados son “preparados” más que completamente productivos.

## 12. Calidad del código

### Fortalezas

- separación real entre master y tenant;
- routers y servicios bastante modularizados;
- jobs para desacoplar trabajo pesado;
- suite mínima de tests existente;
- uso de plantillas parciales en la UI;
- soporte para import/export/learning/diagnóstico.

### Problemas

- algunos servicios son muy grandes: `dashboard/service.py`, `settings/routes.py`, `imports/routes.py`, `orders/routes.py`;
- hay lógica de cálculo y filtrado duplicada entre endpoints;
- muchas vistas siguen siendo densas;
- hay rutas legacy y puentes durante la migración;
- todavía existen campos y helpers preparativos que no están del todo cerrados.

## 13. Rendimiento y escalabilidad

Problemas actuales:
- rutas de dashboard y bandeja con composición pesada;
- varias consultas y conteos por pantalla;
- refresco periódico de alertas;
- carga de detalle operativo bajo demanda, pero con bastante estado alrededor.

Riesgos a corto plazo:
- crecimiento de datos en tenant puede castigar home y workbench;
- IMAP puede bloquear si se usa sin backoff o sin límites;
- algunas vistas hacen más trabajo del necesario para mostrar una lista compacta.

Riesgos al escalar:
- jobs sin observabilidad suficiente;
- dataset por compañía muy grande;
- consultas N+1 si se amplían vistas sin cuidado;
- más canales pueden cargar más el pipeline si no se mantienen por fases.

## 14. Seguridad

Riesgos clasificados:

- **Crítico**: credenciales demo y secretos por defecto si se despliegan sin cambiar; cualquier uso de valores de ejemplo en producción sería un problema serio.
- **Alto**: dependencia de auth por sesión y permisos en muchas rutas; falta rate limiting visible; LLM e IMAP son superficies de entrada externas.
- **Medio**: CORS de desarrollo amplio; logs y diagnósticos deben vigilar no exponer secretos.
- **Bajo**: sanitización visual/HTML en plantillas y previews está razonablemente controlada, pero debe seguir revisándose.

## 15. Pruebas

Estado:
- hay tests reales en `backend/tests/test_core.py`;
- cubren seguridad base, tenancy, jobs, soft delete, diagnóstico, middleware;
- no se ve una suite amplia de integración UI o contratos de rutas completas.

Resultado de ejecución:
- `./.venv/bin/python -m unittest tests.test_core` desde `backend/` ha pasado: **9 tests OK**.

Carencias:
- no se ve cobertura sobre calendario/tareas porque no existe el módulo;
- no hay pruebas visibles para IMAP real, exportaciones reales o flujos end-to-end completos;
- falta cobertura de autorización fina por endpoint.

## 16. Experiencia de usuario

La UX actual es operativa, densa y útil para un operador experto.

Lo positivo:
- acceso rápido a acciones;
- muchas pantallas ya están conectadas;
- bandeja, pedidos, clientes, productos, learning y jobs son usables.

Lo mejorable:
- demasiada densidad en home, settings y detalle de pedido;
- muchas secciones son técnicas;
- la navegación sigue pareciendo panel administrativo más que producto pulido;
- faltan estados de carga y progresividad en algunos puntos.

## 17. Deuda técnica

Prioridad alta:

1. Seguir aligerando home y workbench.
2. Reducir peso de `settings/routes.py`.
3. Seguir separando master vs tenant en todo el runtime.
4. Cerrar módulos legacy/puente.
5. Mejorar observabilidad de jobs y flujos pesados.

Prioridad media:

1. Consolidar importaciones y vistas maestras.
2. Reducir duplicación entre clientes/productos/databases.
3. Simplificar detalle de revisión.
4. Seguir encapsulando lógica de scoring, matching y learning.

## 18. Archivos críticos

| Ruta | Función | Problema | Prioridad |
|---|---|---|---|
| `backend/app/settings/routes.py` | Configuración principal | Demasiada lógica y muchas secciones | Alta |
| `backend/app/dashboard/service.py` | Composición de bandeja/home | Muy cargado, con filtrado y métricas mezcladas | Alta |
| `backend/app/orders/routes.py` | CRUD/revisión de pedidos | Archivo grande, mucha responsabilidad | Alta |
| `backend/app/agent/platform.py` | Pipeline IA común | Muy central; debe mantenerse estable | Alta |
| `backend/app/agent/services.py` | Matching/scoring/procesamiento | Mezcla de mock y flujo real | Alta |
| `backend/app/db/models.py` | Modelo de datos entero | Muy extenso, difícil de navegar | Alta |
| `backend/app/tenancy/database.py` | Resolución tenant | Pieza crítica de aislamiento | Alta |
| `backend/app/master/service.py` | Login y contexto tenant | Base de auth y membresías | Alta |
| `backend/app/channels/routes.py` | Bandeja de canales/correos | Complejo, pero clave para operaciones | Media |
| `backend/app/learning/routes.py` | Aprendizaje y RAG | Funcional, pero aún evolutivo | Media |

## 19. Errores encontrados al ejecutar

1. `./.venv/bin/python -m unittest backend.tests.test_core` desde `backend/`
   - Error: `ModuleNotFoundError: No module named 'backend'`
   - Causa: se invocó el módulo con un path incorrecto desde la carpeta `backend`.

2. `./.venv/bin/python -m unittest tests.test_core`
   - Resultado: correcto.
   - Validación: 9 tests ejecutados con OK.

3. `python3 -m compileall backend/app`
   - Resultado: correcto.

No se detectaron errores de compilación en esta auditoría.

## 20. Elementos que requieren información adicional

No he podido confirmar desde el código:

- si existe CI/CD externa;
- si hay Docker fuera del árbol inspeccionado;
- si hay cobertura de tests fuera de `backend/tests/test_core.py`;
- si hay despliegues reales por cliente ya automatizados;
- si la base de datos de producción usa PostgreSQL en todos los tenants o solo en algunos.

## 21. Plan de estabilización

| Objetivo | Módulos afectados | Dependencia | Riesgo | Esfuerzo | Criterio de aceptación | Orden |
|---|---|---|---|---|---|---|
| Seguir aligerando home | `pages/routes.py`, `dashboard/service.py`, templates de dashboard | Bajo | Bajo | M | Home carga rápido y sin datos innecesarios | 1 |
| Reducir peso de settings | `settings/routes.py`, `settings/*` | Medio | Medio | L | Configuración sigue funcional pero más modular | 2 |
| Cerrar puentes legacy | `legacy/`, `main.py`, rutas compartidas | Medio | Medio | M | Runtime normal sin caminos viejos | 3 |
| Mejorar jobs | `jobs/routes.py`, `jobs/service.py`, worker | Medio | Bajo | M | Ver detalle, reintentar y auditar mejor | 4 |
| Reforzar tenancy | `master/`, `tenancy/`, dependencias | Alto | Alto | L | Aislamiento consistente en todas las rutas | 5 |
| Añadir pruebas de integración críticas | `auth`, `orders`, `learning`, `settings` | Bajo | Bajo | L | Cobertura mínima sobre flujos clave | 6 |

## 22. Roadmap recomendado

### Fase 0. Estabilización
- cerrar puentes legacy;
- asegurar login/master/tenant;
- reforzar jobs y diagnósticos;
- mantener la home ligera.

### Fase 1. Normalización de operación
- consolidar bandeja de pedidos;
- simplificar revisión;
- terminar de separar canal/email/inbound message;
- limpiar settings muy densos.

### Fase 2. Observabilidad y rendimiento
- jobs con detalle, error y reintento;
- carga diferida del detalle;
- métricas y diagnóstico admin;
- backfill IMAP controlado.

### Fase 3. Aprendizaje y conocimiento
- mejorar correcciones, aliases y RAG;
- consolidar aprendizaje humano;
- exportar conocimiento útil sin romper validación determinista.

### Fase 4. Nuevos canales
- WhatsApp;
- voz;
- redes sociales.

### Fase 5. Producto operativo general
- si en el futuro se quieren tareas/proyectos/calendario, habrá que crear ese módulo desde cero con su propio modelo.

## 23. Próximo bloque de trabajo recomendado

El siguiente bloque con más valor técnico es terminar de cerrar la limpieza de la capa operativa: home más ligera, workbench más progresivo y jobs más observables. Después de eso ya tiene mucho sentido seguir con backfill IMAP controlado y la mejora fina de revisión.
