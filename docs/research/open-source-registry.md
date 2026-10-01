# Registro de descubrimiento open source — BLOQUE 0.5

Fecha de corte: 2026-10-01
Repositorio auditado: `/Users/marc/Documents/GEMAVI.worktrees/modular-order-hub`  
Rama: `feat/modular-order-hub`  
Alcance: investigación documental; no se han añadido dependencias, ejecutado código de terceros, conectado servicios externos ni modificado el runtime de ANCHI.

## Criterio de lectura

- **ADOPT**: dependencia o protocolo utilizable directamente, con licencia y acoplamiento compatibles; requeriría una decisión posterior de ingeniería.
- **ADAPT**: patrones o piezas acotadas que pueden inspirar un adaptador propio sin importar el framework anfitrión.
- **REFERENCE**: fuente de diseño, payloads o comportamiento; no se recomienda incorporar el código como dependencia.
- **REJECT**: no recomendable para ANCHI por licencia, estado, acoplamiento, madurez o desalineación funcional.

La clasificación es preliminar. Antes de incorporar cualquier código habría que fijar versión, revisar el fichero de licencia y sus dependencias transitivas, ejecutar una revisión legal y comprobar compatibilidad con el modelo multi-tenant de ANCHI.

## Registro de candidatos

| Nombre | GitHub / fuente | Owner | Función | Lenguaje / framework | Licencia | Estado / actividad observable | Madurez aparente | Dependencia de framework | Partes relevantes | Riesgos | Encaje ANCHI | Clasificación |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Meta WhatsApp API Examples | [fbsamples/whatsapp-api-examples](https://github.com/fbsamples/whatsapp-api-examples) | Meta/Facebook Samples | Ejemplos de webhooks, media, templates y validación de firma | Node.js, Python, Java | Meta Platform Policy; no tratar como SDK redistribuible | Repositorio de ejemplos, no producto; actividad visible limitada frente a un cliente mantenido | Baja-media como base de producción; alta como referencia oficial | Baja en cada ejemplo, pero orientado a la API de Meta | GET de verificación, POST de webhook, `X-Hub-Signature-256`, media y templates | Licencia específica, ejemplos incompletos, evolución de Graph API | Los payloads y el orden de validación son útiles; el transporte propio de ANCHI ya cubre parte del caso | REFERENCE |
| WhatsApp Node.js SDK | [WhatsApp/WhatsApp-Nodejs-SDK](https://github.com/WhatsApp/WhatsApp-Nodejs-SDK) | Meta | SDK y ejemplos de Cloud API | Node.js | Licencia de Meta | Archivado desde 2023 según el repositorio | Baja para una integración nueva | Node.js y API histórica | Verificación de webhook, firma y reintentos básicos | Archivado, sin nuevas correcciones, posible desfase de API | Sirve para contrastar contratos antiguos, no para depender de él | REJECT |
| Frappe WhatsApp | [frappe/whatsapp](https://github.com/frappe/whatsapp) | Frappe | Integración completa de WhatsApp Cloud API: mensajes, templates, media, estados y logs | Python/JavaScript, Frappe | MIT | Repositorio público con 146 commits y actividad visible; documentación y CI | Media-alta para su contexto | Alta: DocTypes, Desk, hooks y APIs whitelisted de Frappe | Modelo de log, estados Sent/Delivered/Read/Failed, perfiles, templates y webhook | Modelo de datos incompatible, migración de conceptos, dependencia Frappe | Patrones de lifecycle y observabilidad adaptables; no importar la aplicación | ADAPT |
| WhatsApp Cloud API PHP SDK | [netflie/whatsapp-cloud-api](https://github.com/netflie/whatsapp-cloud-api) | Netflie | Cliente para enviar/recibir mensajes, media, templates, listas y productos | PHP | MIT | 236 commits, tests y changelog visibles | Media-alta como SDK PHP | Baja respecto a Laravel, pero lenguaje distinto | Mensajes de producto, media upload/download, webhook notifications | No encaja con el backend Python; hay que traducir contratos y seguir cambios de Meta | Buena referencia funcional para catálogo y tipos de mensaje | REFERENCE |
| Frappe WhatsApp alternativo | [shridarpatil/frappe_whatsapp](https://github.com/shridarpatil/frappe_whatsapp) | Comunidad Frappe | Multi-cuenta, conversaciones, templates, flows, media y chatbot | Python, Frappe/ERPNext | MIT | 477 stars y 377 forks visibles; actividad y alcance amplios | Media; debe auditarse por feature antes de reutilizar | Alta: Frappe/ERPNext DocTypes | Recepción de `order`, media por `media_id`, tracking de mensajes | Acoplamiento fuerte, posibles decisiones de dominio no equivalentes, soporte comunitario | Útil para comparar payloads y casos de WhatsApp Commerce; no copiar código | REFERENCE |
| Meta for WooCommerce | [facebook/facebook-for-woocommerce](https://github.com/facebook/facebook-for-woocommerce) | Meta Partner Engineering | Sincroniza catálogo WooCommerce con Meta y habilita ventas/WhatsApp | PHP, WordPress, WooCommerce | GPL-2.0-or-later | 2.785 commits y releases visibles; plugin oficial activo | Alta dentro de WordPress/WooCommerce | Muy alta | Sincronización de productos, logs, recuperación de crashes y eventos de WhatsApp | GPL y acoplamiento WordPress; no es un conector Python aislado | Fuente para escenarios de catálogo y fallos, no dependencia de ANCHI | REFERENCE |
| Shopify Python API | [Shopify/shopify_python_api](https://github.com/Shopify/shopify_python_api) | Shopify | Cliente histórico de Admin API | Python | Revisar licencia del repositorio antes de uso | El README indica que está deprecated, sin nuevas features ni correcciones de seguridad | Baja para una integración nueva | Baja-media | OAuth/sesiones y recursos históricos | Desfase hacia GraphQL Admin API, mantenimiento detenido | No basar el conector nuevo en esta librería | REJECT |
| Odoo WhatsApp Cloud API | [altinkaya-opensource/odoo-whatsapp-cloud-api](https://github.com/altinkaya-opensource/odoo-whatsapp-cloud-api) | Altinkaya Enclosures | Módulo Odoo + UI para WhatsApp, media, status y conversaciones | Python/Odoo, Next.js | LGPL-3 | 165 commits, pero 6 stars y 4 forks visibles | Media-baja fuera de Odoo | Muy alta | Separación backend/frontend, estado de conversación y media | LGPL-3, Odoo addons, Next.js; riesgo de contaminación de licencia y dominio | Referencia de UX y eventos; implementar adaptador contra API oficial | REFERENCE |
| OdooRPC | [OCA/odoorpc](https://github.com/OCA/odoorpc) | Odoo Community Association | Cliente Python para RPC y modelos Odoo | Python | LGPL-3 | Proyecto mantenido por OCA; orientado principalmente a JSON-RPC/XML-RPC | Alta para RPC legacy, no para este protocolo | Media-alta: proxy/model browser y login RPC | Contexto, modelos y métodos | No es un cliente JSON-2 puro; mezclarlo ocultaría el límite de transporte del PoC | Contraste histórico; no incorporar al adapter JSON-2 | REJECT |
| pyodoo-client | [tumbi-j/pyodoo-client](https://github.com/tumbi-j/pyodoo-client) | Comunidad | Cliente Python para `/json/2` Odoo 19+ | Python | Revisar licencia y dependencias antes de adoptar | Proyecto reciente con actividad visible, pero base de adopción pequeña | Pendiente de madurez | Añade ORM wrapper, requests y abstracciones propias | Model calls, context y JSON-2 | Dependencia nueva, contrato de errores propio y posible solapamiento con `httpx` | Referencia para comparar; mantener adapter propio hasta contract verification | REFERENCE |
| Cliente Node de SAP Business One | [syedhussim/node-sapb1](https://github.com/syedhussim/node-sapb1) | Comunidad | Wrapper mínimo de Service Layer de SAP B1 | JavaScript/Node.js | No fijar sin revisar el fichero LICENSE | 14 stars, 6 commits y 5 forks visibles | Baja | Baja, pero wrapper muy delgado | Login de sesión y `Orders` create/update | Poca cobertura, posible abandono, contrato SAP cambiante | La API oficial de SAP es mejor fuente; usar el wrapper solo para contraste | REJECT |
| Simple Salesforce | [simple-salesforce/simple-salesforce](https://github.com/simple-salesforce/simple-salesforce) | Comunidad | Cliente Python REST/APEX para Salesforce | Python | Apache-2.0 según README | 496 commits, 725 forks, soporte Python 3.9–3.13 visible | Media-alta como cliente de bajo nivel | Baja | OAuth, REST, Apex y consultas | No resuelve modelo de negocio, límites, Pub/Sub ni multi-tenant por sí solo | Posible dependencia acotada, pendiente de revisión legal y de versión | ADOPT |
| Nango | [nangohq/nango](https://github.com/nangohq/nango) | Nango | Plataforma de integraciones, OAuth, refresh, retries y 900+ APIs | TypeScript, runtime propio | Elastic License | 11,8k stars y 1,3k forks visibles; activo | Alta como plataforma, no necesariamente como librería embebible | Muy alta: runtime, API y modelo operativo Nango | Gestión de credenciales, conexiones, reintentos y rate limits | ELv2 limita ofrecer el producto como servicio gestionado; sobrealcance para ANCHI | Buen catálogo conceptual y referencia de operación; no adoptar como runtime | REFERENCE |
| Airbyte | [airbytehq/airbyte](https://github.com/airbytehq/airbyte) | Airbyte | ELT/data movement y catálogo de conectores | Java/Kotlin/Python/TypeScript, plataforma propia | ELv2 para repos públicos y conectores salvo protocolo MIT; Cloud/Enterprise comercial | Catálogo de 600+ conectores y actividad alta | Alta como plataforma de datos | Muy alta | CDK, manifests, discover/check/read y conectores SaaS | No es un bus transaccional de pedidos; ELv2 y coste operativo; sincronización distinta de eventos | Puede inspirar contratos batch, no debe convertirse en dependencia del core | REJECT |
| Sage X3 sample web portal | [Sage-ERP-X3/sample-x3-ws-php-webportal](https://github.com/Sage-ERP-X3/sample-x3-ws-php-webportal) | Sage-ERP-X3 | Portal de ejemplo para SOAP, GraphQL y aplicaciones conectadas de Sage X3 | PHP, Docker | No se ha confirmado un fichero de licencia utilizable para redistribución | Repositorio oficial de ejemplos; README lo marca como presales/developer y no producción | Media como referencia, baja como base de producto | Acoplado a X3, PHP y configuración de servidor | JWT/connected apps, consultas GraphQL, pedidos/stock/productos de X3 | No es Sage 200 España; ejemplo no productivo; código y patch X3 no se deben incorporar sin revisión | Puede orientar documentación de X3 si se aborda en otra fase | REFERENCE |
| Sagekit | [lepepe/sagekit](https://github.com/lepepe/sagekit) | Comunidad | Cliente Ruby para Sage 300 Web API | Ruby | MIT | 36 commits y documentación del cliente visibles | Media para Sage 300, baja para ANCHI | Ruby y endpoints Sage 300 | Empresa en URL, OData, paginación, recursos de ventas | Producto/mercado distintos; no resuelve Sage 200 ES ni el contrato ANCHI | Solo contraste conceptual de cliente HTTP y paginación | REJECT |
| Sage 50 Accounts SDK Tools | [Dankular/Sage50Accounts](https://github.com/Dankular/Sage50Accounts) | Comunidad | REST wrapper local sobre SDO/COM para Sage 50 Accounts UK | C#/.NET/Windows | MIT; usa SDK/COM de Sage con términos propios a revisar | Repositorio pequeño, 23 commits y 3 stars visibles | Baja para un servicio cloud/multitenant | Windows, ruta ACCDATA, SDK local y una sesión por usuario | Clientes, productos, stock y postings locales | Reino Unido, desktop, dependencia SDO, no API española pública | No adoptar; como máximo evidencia de que “Sage 50 API” puede significar SDK local | REJECT |
| Sage X3 API profile | [api-evangelist/sage-x3](https://github.com/api-evangelist/sage-x3) | API Evangelist | Inventario independiente de APIs y documentación Sage X3 | YAML/Markdown | Repositorio de referencia; no fuente contractual de Sage | Actividad visible, pero tercero no oficial | Media como índice, no como SDK | Ninguno en ANCHI | Enlaces a REST, GraphQL, bulk y organización oficial | Puede mezclar inferencias con fuentes; no demuestra Sage 200 ES | Útil para localizar fuentes oficiales, no para copiar contratos | REFERENCE |

## Hallazgos por integración objetivo

### WhatsApp Cloud API y Meta Commerce

La fuente de verdad es la API oficial de Meta y sus payloads, no un SDK comunitario. La verificación inicial debe ser barata y síncrona: GET con `hub.verify_token` y respuesta `hub.challenge`; para POST debe validarse el HMAC de `X-Hub-Signature-256` sobre el cuerpo crudo antes de persistir o encolar. La respuesta de recepción debe ser rápida y el procesamiento pesado debe ser asíncrono.

Los mensajes de pedido y catálogo requieren conservar el payload original. Un pedido recibido desde WhatsApp puede contener `catalog_id`, `product_items`, identificadores de producto, cantidad y precio; esos datos deben entrar como evidencia de origen y después mapearse a `Order`/`OrderLine`, sin confundir el ID del catálogo externo con el `product_id` interno. Los cambios de inventario y disponibilidad de un catálogo son candidatos a sincronización separada, no motivo para contaminar el pipeline de extracción textual.

### WooCommerce

La REST API v3 cubre productos, pedidos, clientes, lotes y webhooks. Los webhooks incluyen topic, delivery ID y firma HMAC `X-WC-Webhook-Signature`, pero WooCommerce puede deshabilitarlos después de entregas fallidas consecutivas y no garantiza entrega perfecta. ANCHI debería conservar el `delivery_id`, responder pronto, deduplicar y disponer de reconciliación por `updated_at` o consulta periódica.

### Shopify

Para nuevas integraciones debe priorizarse GraphQL Admin API. Para un pedido iniciado en chat o teléfono, `draftOrderCreate` es más próximo al caso que crear directamente un pedido final. Shopify documenta firma HMAC, delivery IDs, duplicados, orden no garantizado, reintentos limitados y la necesidad de reconciliación. Las mutaciones de salida deben llevar una clave de idempotencia propia, aunque el proveedor no la exponga uniformemente para todos los recursos.

### Odoo

Odoo ofrece JSON-2 con bearer API key y endpoints por modelo/método; las APIs XML-RPC/JSON-RPC antiguas tienen una retirada anunciada en versiones futuras. El conector debe aislar el protocolo y negociar capacidades por versión/edición: clientes, productos, pedidos, partners, stock y documentos no deben asumirse iguales entre instalaciones.

Para BLOQUE 6 se elige la API oficial JSON-2 de Odoo 19+ y el cliente HTTP `httpx` ya presente en el proyecto. No se añade un SDK Odoo comunitario: el endpoint es suficientemente simple para mantener el transporte aislado y evitar una dependencia que mezcle XML-RPC/JSON-RPC legacy. `OdooRPC` queda descartado para este PoC por protocolo; `pyodoo-client` queda como referencia reciente, no como dependencia, hasta auditar madurez, licencia y contrato de errores. El adapter propio sobre `httpx` queda como opción recomendada tras verificar `/doc` y el plan del cliente. No se copia código externo.

### SAP Business One y otros SAP

Para SAP Business One la fuente primaria es Service Layer, OData v4 en `/b1s/v2` y v3 en `/b1s/v1` con compatibilidad condicionada por patch level. El conector debe encapsular login/sesión, cookies `B1SESSION`/`ROUTEID`, paginación, PATCH/POST, errores de negocio y expiración de sesión. No se recomienda depender de wrappers comunitarios pequeños.

Para SAP no-B1 hay que tratar BAPI, OData, IDoc y APIs de cada producto como familias distintas. Queda fuera de este bloque seleccionar una tecnología SAP concreta.

### Sage

Sage Active expone GraphQL y OAuth 2.0 con `x-api-key`, además de clientes, productos, documentos de ventas/compras, stock y contabilidad. Sage X3 mezcla REST/SOAP y, según el flujo, GraphQL o intercambio de ficheros. Sage 200 UK tiene APIs REST versionadas. La arquitectura debe modelar capacidades y versionado por producto, sin un genérico “Sage” que oculte diferencias incompatibles.

### Salesforce

Salesforce ofrece REST, upsert por External ID, Composite/SObject Collections y Pub/Sub API para Platform Events y Change Data Capture. El `ReplayId` permite reanudar streams durante la retención, pero no elimina la necesidad de deduplicación y reconciliación. Un adaptador de salida puede utilizar upsert por External ID; uno de entrada puede consumir eventos solo si el tenant tiene edición, permisos y cuota compatibles.

## Informe final de BLOQUE 0.5

### A. Top findings

1. ANCHI ya tiene el esqueleto correcto: entrada común, almacenamiento de evidencia, pipeline compartido, revisión, aprendizaje y exportación.
2. El hueco principal no es otro parser, sino una frontera explícita entre conectores externos, eventos durables y el dominio canónico de pedidos.
3. Meta, WooCommerce, Shopify, Salesforce y ERP ofrecen entrega o APIs con duplicados, límites, expiraciones y orden parcial; “exactly once” no puede suponerse como propiedad del proveedor.
4. Nango y Airbyte son plataformas completas, no librerías pequeñas. Sus licencias y su coste de operación hacen prudente mantenerlos fuera del core.
5. El SDK oficial de WhatsApp para Node está archivado y la librería Python de Shopify está deprecated; no deben guiar la elección de dependencias nuevas.

### B. Qué conservar de ANCHI

- `company_id` como frontera de tenant.
- `InboundMessage`, `MessageAttachment` y `NormalizedInput` como evidencia y normalización común.
- `Order`, `OrderLine`, matching de clientes/productos, scoring, `OrderReview`, correcciones y aprendizaje.
- `BackgroundJob`, `ExportJob`, workers, `AuditLog` y el workbench existente.
- La regla arquitectónica de no hardcodear credenciales, prompts, scoring o formatos por tenant.

### C. Qué extender

- El contrato de canal para añadir capacidades, firma, recepción rápida, deduplicación, paginación y estado de conexión.
- El dominio canónico con referencias externas, fuente, versión de esquema, snapshot de entrada y trazabilidad por línea.
- El job system con eventos de outbox, intentos de entrega, backoff con jitter, límites por proveedor, circuit breaker, DLQ y reconciliación.
- La extracción multimodal: PDF/OCR/STT deben ser adaptadores reales detrás de una interfaz, no lógica en el conector.
- El exportador para que cada ERP/commerce sea un connector de salida con idempotencia y reconciliación.

### D. Qué crear de nuevo

- Registro de conectores por tenant y capacidades declaradas.
- Identidad externa por entidad y tabla de mapeo de IDs.
- Inbox de eventos entrantes y outbox de efectos salientes.
- Delivery attempts, estados `pending/succeeded/retryable/failed/dead_lettered`, y auditoría de request/response sin secretos.
- Motor de reconciliación por ventana/`updated_at` y panel operativo para reintentos y DLQ.
- Normalizador específico para mensajes estructurados de catálogo/pedido separado del texto libre.

### E. Dependencias candidatas

- **Posible ADOPT:** `simple-salesforce` como cliente REST Python acotado, después de revisar licencia, versión, límites y política de actualizaciones.
- **ADAPT/REFERENCE:** patrones de `frappe/whatsapp`, ejemplos Meta, `netflie/whatsapp-cloud-api` y `facebook-for-woocommerce`.
- **No adoptar:** Shopify Python API deprecated, WhatsApp Node SDK archivado, wrappers SAP mínimos, Nango y Airbyte como runtime.
- **No copiar código:** Meta samples y plugins integrales de WordPress/Odoo/Frappe; deben usarse para comportamiento observable y diseño.

### F. Referencias de código ANCHI

- `backend/app/agent/platform.py:67-170`: `InboundPayload`, registro de canales, Email implementado y WhatsApp/voz/social aún stub; PDF/OCR/STT son placeholders.
- `backend/app/agent/platform.py:249-360`: matching de clientes y productos.
- `backend/app/agent/platform.py:1233-1323`: exportación y entrada al pipeline común.
- `backend/app/whatsapp/routes.py:438-767`: verificación y recepción real de webhooks WhatsApp.
- `backend/app/whatsapp/service.py:1276-1280`: verificación de token y firma.
- `backend/app/db/models.py:773-924`: mensajes, attachments, normalized inputs, revisión, correcciones y aprendizaje.
- `backend/app/db/models.py:967-990`: jobs con dedupe, reintentos, locks y `next_retry_at`.
- `backend/app/db/models.py:1100-1175`: pedido canónico y líneas.
- `backend/app/db/models.py:1198-1208`: auditoría.
- `ARCHITECTURE.md`: secuencia común, contrato de canal, contrato de servicios y regla multi-tenant.

### G. Riesgos de licencia

- GPL-2.0-or-later del plugin Meta for WooCommerce y LGPL-3 del módulo Odoo requieren análisis de distribución y enlace antes de reutilizar código.
- ELv2 de Nango y Airbyte no equivale a MIT/Apache: permite uso con restricciones concretas, especialmente si ANCHI ofreciera el runtime o producto de integración como servicio.
- Meta Platform Policy y las licencias de los samples no deben interpretarse como permiso para empaquetar código de ejemplo como SDK.
- MIT/Apache-2.0 son más compatibles, pero no eliminan obligaciones de notices, dependencias transitivas, trademarks, términos de API, datos personales o políticas de proveedor.

### H. Arquitectura preliminar

La propuesta detallada está en [preliminary-architecture.md](./preliminary-architecture.md). En síntesis: conectores de entrada/salida desacoplados, inbox/outbox persistentes, procesamiento estructurado y no estructurado separados, dominio canónico como frontera, validación y workbench humanos, y routing de salida por capacidades.

### I. Propuesta de BLOQUE 1 — sin implementar ahora

1. Acordar el contrato `InputConnector`/`OutputConnector`, capabilities, eventos y estados.
2. Diseñar las migraciones mínimas para identidad externa, inbox, outbox, attempts y reconciliación.
3. Implementar primero un vertical de bajo riesgo con fixtures: WhatsApp estructurado + texto libre hacia el dominio canónico, sin salida externa.
4. Añadir un conector de salida en modo sandbox/mock antes de tocar cuentas reales.
5. Incorporar pruebas de contrato, duplicados, firma, orden, reintento, rate limit, timeout, sesión expirada y DLQ.
6. Elegir solo después las dependencias definitivas y registrar notices/licencias.

## Proveniencia

Consulta web realizada el 2026-09-30. Fuentes primarias y repositorios consultados:

- [Meta WhatsApp API examples](https://github.com/fbsamples/whatsapp-api-examples)
- [Meta WhatsApp Node.js SDK](https://github.com/WhatsApp/WhatsApp-Nodejs-SDK)
- [Frappe WhatsApp](https://github.com/frappe/whatsapp)
- [Netflie WhatsApp Cloud API](https://github.com/netflie/whatsapp-cloud-api)
- [Frappe WhatsApp alternativo](https://github.com/shridarpatil/frappe_whatsapp)
- [Meta for WooCommerce](https://github.com/facebook/facebook-for-woocommerce)
- [WooCommerce REST API](https://developer.woocommerce.com/docs/apis/rest-api/)
- [WooCommerce webhooks](https://developer.woocommerce.com/docs/apis/rest-api/v2/webhooks)
- [Shopify GraphQL `draftOrderCreate`](https://shopify.dev/docs/api/admin-graphql/latest/mutations/draftordercreate)
- [Shopify webhook verification](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries)
- [Odoo JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)
- [SAP Business One Service Layer](https://help.sap.com/doc/056f69366b5345a386bb8149f1700c19/10.0/en-US/Service%20Layer%20API%20Reference.html)
- [Sage Active API](https://developer.sage.com/active)
- [Sage X3 API guide](https://developer.sage.com/x3/docs/latest/guides/x3-development-platform/platform-reference/api-guide)
- [Salesforce REST upsert by External ID](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-patch.html)
- [Salesforce Pub/Sub API](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/intro.html)
- [Simple Salesforce](https://github.com/simple-salesforce/simple-salesforce)
- [Nango](https://github.com/nangohq/nango)
- [Airbyte licenses](https://github.com/airbytehq/airbyte/blob/master/docs/community/licenses/README.md)
- [AWS cloud design patterns](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/introduction.html)
- [AWS transactional outbox](https://aws.amazon.com/blogs/compute/implementing-the-transactional-outbox-pattern-with-amazon-eventbridge-pipes/)
- [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests)
- [GitHub webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)

Los datos de stars, forks, commits, estado de archivo y actividad son observaciones de las páginas consultadas en esa fecha, no una garantía de soporte futuro.
