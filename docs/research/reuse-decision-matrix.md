# Matriz de decisión de reutilización

Fecha: 2026-09-30  
Regla: la acción es una recomendación de diseño, no una autorización para instalar ni copiar código.

| Funcionalidad | Repositorio / solución | Licencia | Madurez | Acoplamiento | Acción | Motivo |
|---|---|---|---|---|---|---|
| Verificación y payloads WhatsApp | [Meta API examples](https://github.com/fbsamples/whatsapp-api-examples) | Meta Platform Policy | Media como ejemplos | Bajo por ejemplo | REFERENCE | Contrastar handshake, firma y formas de payload; no empaquetar samples |
| SDK WhatsApp Node | [WhatsApp Node SDK](https://github.com/WhatsApp/WhatsApp-Nodejs-SDK) | Meta | Baja: archivado | Node.js | REJECT | No sirve como base de una integración nueva |
| Mensajería y lifecycle WhatsApp | [Frappe WhatsApp](https://github.com/frappe/whatsapp) | MIT | Media-alta en su contexto | Alto: Frappe | ADAPT | Adoptar conceptos de estados, logs y templates; reimplementar contrato en ANCHI |
| Mensajes de catálogo/producto WhatsApp | [Netflie SDK](https://github.com/netflie/whatsapp-cloud-api) | MIT | Media-alta | PHP | REFERENCE | Buena cobertura funcional, pero no compatible con runtime Python |
| WhatsApp con ERP | [shridarpatil/frappe_whatsapp](https://github.com/shridarpatil/frappe_whatsapp) | MIT | Media | Frappe/ERPNext | REFERENCE | Comparar `order`, media y multi-cuenta; no importar dominio Frappe |
| Meta Catalog/WooCommerce | [Meta for WooCommerce](https://github.com/facebook/facebook-for-woocommerce) | GPL-2.0-or-later | Alta | WordPress/WooCommerce | REFERENCE | Fuente de escenarios de sincronización, no dependencia por licencia/acoplamiento |
| REST WooCommerce | [WooCommerce REST API v3](https://developer.woocommerce.com/docs/apis/rest-api/v3/) | API oficial; SDK según cliente | Alta | HTTP/WordPress solo en servidor | NEW CONNECTOR | Crear adaptador ANCHI con HMAC webhooks, paginación, estados y reconciliación |
| Admin Shopify | [Shopify GraphQL Admin API](https://shopify.dev/docs/api/admin-graphql/latest/mutations/draftordercreate) | API oficial / términos Shopify | Alta | HTTP/GraphQL | NEW CONNECTOR | Preferir GraphQL; `draftOrderCreate` encaja con pedidos originados en conversación |
| Cliente Python Shopify histórico | [shopify_python_api](https://github.com/Shopify/shopify_python_api) | Revisar repo | Baja: deprecated | Bajo, pero obsoleto | REJECT | README indica ausencia de nuevas features y security fixes |
| WhatsApp/Odoo | [odoo-whatsapp-cloud-api](https://github.com/altinkaya-opensource/odoo-whatsapp-cloud-api) | LGPL-3 | Media-baja fuera de Odoo | Muy alto | REFERENCE | Sirve para comparar UI/status/media; riesgo LGPL-3 y modelo Odoo |
| API Odoo | [JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html) | API oficial / términos Odoo | Alta como contrato | HTTP/model-method | NEW CONNECTOR | Aislar versión, API key, modelo, método y capacidades por instalación |
| SAP B1 | [Service Layer API](https://help.sap.com/doc/056f69366b5345a386bb8149f1700c19/10.0/en-US/Service%20Layer%20API%20Reference.html) | API/proveedor SAP | Alta como fuente primaria | HTTP/OData/sesión | NEW CONNECTOR | Usar fuente oficial; soportar `B1SESSION`, `ROUTEID`, OData y patch levels |
| Wrapper SAP B1 | [node-sapb1](https://github.com/syedhussim/node-sapb1) | Revisar LICENSE | Baja: 14 stars/6 commits | Node.js | REJECT | Poca cobertura y madurez; no sustituye documentación SAP |
| Salesforce CRUD/upsert | [REST API + External ID](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-patch.html) | API oficial / términos Salesforce | Alta | HTTP/OAuth | NEW CONNECTOR | External ID permite convergencia e idempotencia lógica |
| Salesforce Python client | [simple-salesforce](https://github.com/simple-salesforce/simple-salesforce) | Apache-2.0 según README | Media-alta | Bajo | ADOPT | Único candidato directo razonable; validar versión, seguridad y límites antes de instalar |
| Salesforce eventos | [Pub/Sub API](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/intro.html) | API oficial / edición Salesforce | Alta, con requisitos | gRPC/HTTP2, permisos y cuotas | NEW CONNECTOR | Entrada CDC/Platform Events solo como capability opcional y con replay |
| Auth/conectores genéricos | [Nango](https://github.com/nangohq/nango) | Elastic License | Alta como plataforma | Muy alto | REFERENCE | Inspiración para OAuth/refresh/retries; ELv2 y runtime exceden el core |
| ELT y catálogo de conectores | [Airbyte](https://github.com/airbytehq/airbyte) | ELv2 salvo protocolo MIT | Alta como plataforma | Muy alto | REJECT | No es transaccional; licencia y operación no encajan con el hub de pedidos |
| Outbox, retry, DLQ | [AWS cloud patterns](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/introduction.html) | Documentación | Alta como patrón | Independiente | ADAPT | Llevar el patrón a SQLAlchemy/worker sin adoptar AWS |
| Outbox operativo | [AWS transactional outbox](https://aws.amazon.com/blogs/compute/implementing-the-transactional-outbox-pattern-with-amazon-eventbridge-pipes/) | Documentación / sample | Alta como patrón | AWS en la implementación de ejemplo | REFERENCE | Separar patrón conceptual de infraestructura AWS |
| Idempotencia de mutaciones | [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests) | Documentación | Alta como patrón | Independiente | ADAPT | Persistir clave, hash de payload y resultado; repetir de forma segura |
| Webhooks at-least-once | [GitHub webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks) | Documentación | Alta | Independiente | ADAPT | Firma, respuesta rápida, cola, redelivery y protección de replay |

## Decisión global

La recomendación es **conservar el dominio y workbench de ANCHI, adaptar patrones de fiabilidad y crear conectores propios contra APIs oficiales**. `simple-salesforce` es el único candidato de librería de cliente que merece una evaluación de adopción posterior; incluso ahí la primera implementación debe poder sustituirse por HTTP directo mediante una interfaz estable.

No se recomienda convertir ANCHI en una instancia embebida de Frappe, Odoo, Nango o Airbyte. El hub debe mantener su propio modelo canónico, sus tenants, su trazabilidad y su ciclo humano de revisión.

## Proveniencia y controles pendientes

Las licencias y estados se consultaron en las páginas enlazadas el 2026-09-30. Antes de cualquier adopción: fijar commit/version, guardar notice, revisar dependencia transitiva, verificar si la licencia cambió, documentar datos personales tratados y confirmar términos de cada API. No se ha copiado ni ejecutado código de estos repositorios.
