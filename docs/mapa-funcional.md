# Mapa funcional de la aplicación

## Inventario funcional

| Módulo | Funcionalidad | Estado | Relaciones entre módulos | Flujo principal |
|---|---|---|---|---|
| Auth | Login/logout | Operativa | Master DB, sesión, tenant | Credenciales -> sesión -> tenant |
| Master | Empresas, usuarios, memberships, tenant DBs | Operativa | Auth, tenancy, provisioning | Crear/validar membresías y resolver DB |
| Tenancy | Resolver tenant por request | Operativa | Middleware, auth, DB tenant | Sesión/host -> DB activa |
| Home / Workbench | Bandeja de pedidos y correos | Operativa con limitaciones | Orders, channels, jobs, alerts | Lista operativa -> detalle bajo demanda |
| Pedidos | Revisión y edición | Operativa | Customers, products, learning, export | Abrir pedido -> corregir -> confirmar/exportar |
| Histórico | Visión de pedidos/correos antiguos | Operativa | Orders, emails, pagination | Filtrar histórico -> abrir detalle |
| Canales | Bandeja unificada de entradas | Operativa con limitaciones | Email, inbound messages, orders | Entradas -> normalización -> listado |
| Correo | IMAP, SMTP, lectura, preview | Operativa con limitaciones | Settings, channels, jobs, orders | Leer IMAP -> guardar email -> procesar |
| Clientes | Maestro de clientes y conocimiento | Operativa | Orders, learning, databases | Crear/importar -> vincular -> usar en matching |
| Productos | Maestro de productos y alias | Operativa | Orders, learning, databases | Crear/importar -> vincular -> usar en matching |
| Importación | CSV/Excel/table paste | Operativa con limitaciones | Customers, products, databases | Subir/pegar -> preview -> validar -> confirmar |
| Jobs | Cola, monitor, retry | Operativa | Workbench, imports, settings, workers | Encolar -> ejecutar -> monitorizar |
| Alertas | Centro de alertas | Operativa | Orders, jobs, email, admin | Detectar -> mostrar -> resolver |
| Learning | Correcciones, aliases, RAG | Operativa con limitaciones | Orders, customers, products, prompts | Corregir -> aprender -> aprobar |
| Settings | Configuración del cliente | Operativa con limitaciones | Master, email, AI, export, branding | Editar -> test -> guardar |
| Admin | Diagnóstico y tenants | Operativa | Master, tenant DB, health | Diagnóstico -> estado -> mantenimiento |
| Logs | Auditoría de acciones | Operativa | Todas las áreas | Registrar -> consultar |
| Health | Estado técnico | Operativa | Master, tenant, admin | Verificar -> diagnosticar |
| Proyectos/tareas/calendario | No existe todavía | No encontrada | Ninguna | No hay flujo real |
| Imputación de horas | No existe todavía | No encontrada | Ninguna | No hay flujo real |
| WhatsApp/voz/social | Abstracción preparada | Preparada pero no conectada | Agent platform, channels | Falta implementación real |

## Relaciones clave

- `auth` depende de `master`.
- `master` define compañías, usuarios, membresías y tenant DBs.
- `tenancy` resuelve la base tenant de cada request.
- `workbench`, `orders`, `customers`, `products`, `learning`, `settings`, `imports`, `jobs`, `alerts` viven sobre tenant DB.
- `channels` unifica correo y futuras entradas multicanal.
- `agent/platform.py` es el núcleo compartido de entrada -> normalización -> clasificación -> extracción -> matching -> scoring -> revisión -> aprendizaje.

## Flujos principales

### Flujo de autenticación
`/login` -> `authenticate_master_user()` -> sesión -> `load_tenant_context()` -> `get_tenant_db()` -> runtime del tenant.

### Flujo de pedidos
IMAP o entrada manual -> `InboundMessage`/`Email` -> pipeline de agente -> `Order` + `OrderLine` -> scoring -> revisión -> exportación/learning.

### Flujo de revisión
`/` o `/orders/{id}` -> corrección cliente/producto/estado -> `ManualCorrection` / `LearnedAlias` / `OrderReview` -> aprendizaje.

### Flujo de importación
Archivo o texto pegado -> preview -> mapeo/validación -> creación o actualización de clientes/productos -> logs de importación.

### Flujo de jobs
Acción en UI -> `enqueue_job()` -> `BackgroundJob` -> worker -> success/fail/retry -> monitor.

### Flujo de alertas
Evento operativo o error -> `Alert` -> drawer y `/alerts` -> marcar vista / resolver / reabrir.

## Diagrama textual de arquitectura

```text
Usuario
  -> Login
  -> Master DB (users, memberships, companies, tenant_databases)
  -> Session
  -> Middleware de branding + tenant
  -> Tenant DB (customers, products, emails, orders, learning, alerts, jobs)
  -> UI SSR (Jinja)
  -> Workers (email/jobs)
  -> Integraciones externas (IMAP, SMTP, OpenAI, FTP/SFTP)
```

## Lectura rápida del estado

- El núcleo real es pedidos + correo + learning + jobs.
- La capa de multitenencia ya está en producción técnica.
- La agenda/proyectos/tareas no existen todavía como módulo de negocio.
- WhatsApp/voz/social están previstos, no conectados.
- El sistema ya es una plataforma, aunque todavía con bastante densidad interna.
