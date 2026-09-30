# Análisis de gaps ANCHI frente al descubrimiento open source

Fecha: 2026-09-30  
Principio: el gap analysis propone fronteras y prioridades; no autoriza todavía cambios de código.

| Área | ANCHI actual | Open source / fuente encontrada | Acción propuesta |
|---|---|---|---|
| Multi-tenant | `company_id` está presente en modelos y servicios; es la frontera central | No se necesita una plataforma externa; Nango ofrece aislamiento como servicio, pero con runtime y ELv2 | KEEP |
| Entrada email | `EmailIngestionService` y sincronización IMAP; `Email`/`EmailSettings` y workers existentes | Patrones de webhooks y colas son aplicables, no otro cliente necesario | KEEP |
| Entrada WhatsApp | Rutas reales de verificación/recepción y servicio de envío; el `WhatsAppIngestionService` genérico sigue siendo stub | Meta samples, Frappe WhatsApp, Netflie y Frappe alternativo documentan payloads, media, templates y estados | EXTEND |
| Webhook security | Token y HMAC ya existen en `app/whatsapp/service.py`; recepción en `app/whatsapp/routes.py` | Meta y GitHub confirman HMAC sobre cuerpo crudo; Shopify/Woo añaden delivery IDs | EXTEND |
| Deduplicación de entrada | `InboundMessage` tiene unicidad por tenant/canal/provider/source ID; `BackgroundJob` tiene `dedupe_key` | Woo delivery ID, Shopify webhook ID, Salesforce `ReplayId` y patrones at-least-once | EXTEND |
| Inbox durable | Hay persistencia de `InboundMessage`, pero no una entidad explícita de evento/attempt de entrega | AWS transactional outbox, Microsoft Graph y GitHub recomiendan persistir/encolar y responder rápido | NEW |
| Texto libre | Normalización y extracción de pedido en `platform.py`; matching y scoring existentes | No conviene importar un framework de extracción externo; usar APIs como fuentes de contratos | KEEP |
| PDF / imagen / OCR | `MessageAttachment` contempla extracción, OCR y transcripción, pero los servicios genéricos son placeholders | Muestras de media WhatsApp y apps Frappe muestran lifecycle de archivos, no sustituyen el pipeline | EXTEND |
| Audio / STT | Campo y placeholder `SpeechToTextService`; WhatsApp media soporta audio | Mantener proveedor de STT detrás de adapter; no acoplarlo al conector WhatsApp | EXTEND |
| Pedido estructurado WhatsApp | No hay dominio específico para `catalog_id`/`product_items` como evidencia estructurada | Meta Commerce, Netflie y Frappe alternativo cubren mensajes de catálogo/orden | NEW |
| Dominio canónico | `Order` y `OrderLine` existen, con scores, estados y referencias internas | Salesforce External ID y modelos de comercio sugieren IDs externos, snapshots y estados por proveedor | EXTEND |
| Identidad externa | Hay `source_external_id` para mensajes, pero no una tabla general de entidad externa por sistema | Salesforce upsert, Shopify delivery IDs, Woo IDs y SAP claves externas | NEW |
| Clientes y aliases | `Customer`, aliases, matching y aprendizaje ya existen | APIs de Odoo/Sage/Salesforce/Woo/Shopify aportan fuentes de maestros, no reemplazan el matching | KEEP |
| Productos y catálogo | `Product`, aliases, embeddings y conocimiento ya existen | Meta Catalog, Shopify ProductVariant/InventoryItem, Woo products y ERP product APIs | EXTEND |
| Validación humana | `OrderReview`, workbench y dashboard existen | Frappe/Odoo muestran logging/conversación; no ofrecen una razón para reemplazar el workbench | KEEP |
| Auditoría | `AuditLog` existe, además de logs de WhatsApp y jobs | Frappe WhatsApp, Woo y AWS refuerzan la necesidad de request/response, retries y resultado | EXTEND |
| Jobs y reintentos | `BackgroundJob` ya tiene attempts, retries, lock, heartbeat y `next_retry_at` | AWS retry/backoff/outbox/DLQ; Zendesk/Microsoft documentan circuit breakers y timeouts | EXTEND |
| Outbox de salida | `ExportJob` y exportador CSV/FTP/SFTP existen, pero no hay outbox transaccional general | AWS transactional outbox + Stripe idempotency | NEW |
| WooCommerce salida | No existe conector; solo exportación genérica | Woo REST v3, webhooks HMAC, logs y estados paused/disabled | NEW |
| Shopify salida | No existe conector | GraphQL Admin, `draftOrderCreate`, webhooks HMAC y rate limits | NEW |
| Odoo salida | No existe conector | Odoo JSON-2 por modelo/método y API keys | NEW |
| SAP Business One salida | No existe conector | Service Layer OData v4/v3, sesión y entidades Orders/BusinessPartners | NEW |
| Sage salida | No existe conector | Sage Active GraphQL; Sage X3 REST/SOAP/GraphQL; Sage 200 REST | NEW |
| Salesforce salida/entrada | No existe conector | REST upsert por External ID, Composite, Pub/Sub API y CDC | NEW |
| Registro de conectores | `ChannelIngestionService` tiene registry en memoria; no hay capabilities ni lifecycle común de output | Nango propone catálogo/auth/runtime; Airbyte propone check/discover/read, pero son plataformas sobredimensionadas | EXTEND |
| Auth y secretos | Configuración por tenant/canal ya existe; secretos no deben entrar en logs | Nango aporta refresh/OAuth como referencia, no como dependencia | EXTEND |
| Reconciliación | No se observa un proceso común por `updated_at`/cursor para sistemas externos | Shopify, Woo, Salesforce y Sage requieren relectura/replay/reconciliación | NEW |
| Rate limits | No hay política transversal por proveedor en el dominio canónico | Shopify cost-based, Salesforce allocations, backoff de AWS/Stripe | NEW |
| Circuit breaker | Jobs tienen reintentos, pero no estado de salud por proveedor | Zendesk y AWS documentan circuit breaker y aislamiento | NEW |
| DLQ operativa | Hay estados de error en jobs/export, sin cola de mensajes fallidos con replay controlado | AWS EventBridge Pipes, Unity y patrones webhook | NEW |
| Workbench | `backend/app/workbench` y dashboard ya cubren revisión operativa | No reemplazar por Desk de Frappe ni UI de Odoo | KEEP |
| Arquitectura por capas | `ARCHITECTURE.md` ya define entrada → normalización → extracción → matching → scoring → revisión/exportación → aprendizaje | Hexagonal, outbox y anti-corruption layer de AWS aportan vocabulario | KEEP |

## Lectura de prioridad

1. **Primero**: contrato de conector + inbox/outbox + identidad externa + deduplicación.
2. **Después**: pedido estructurado de WhatsApp y extracción multimodal con evidencia.
3. **Luego**: primer output connector sandbox/mock y reconciliación.
4. **Finalmente**: familias ERP/commerce por proveedor y versión, con capacidades explícitas.

La mayor parte de lo que existe debe mantenerse como dominio y workbench. Las piezas open source encontradas son útiles para contratos, payloads y operación, pero no justifican sustituir el núcleo de ANCHI.

## Proveniencia

Fuentes externas: [Meta WhatsApp examples](https://github.com/fbsamples/whatsapp-api-examples), [Frappe WhatsApp](https://github.com/frappe/whatsapp), [WooCommerce REST](https://developer.woocommerce.com/docs/apis/rest-api/), [WooCommerce webhooks](https://developer.woocommerce.com/docs/apis/rest-api/v2/webhooks), [Shopify draft orders](https://shopify.dev/docs/api/admin-graphql/latest/mutations/draftordercreate), [Shopify webhooks](https://shopify.dev/docs/apps/build/webhooks), [Odoo JSON-2](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html), [SAP Service Layer](https://help.sap.com/doc/056f69366b5345a386bb8149f1700c19/10.0/en-US/Service%20Layer%20API%20Reference.html), [Sage Active](https://developer.sage.com/active), [Sage X3](https://developer.sage.com/x3/docs/latest/guides/x3-development-platform/platform-reference/api-guide), [Salesforce External ID](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-patch.html), [Salesforce Pub/Sub](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/intro.html), [AWS patterns](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/introduction.html), [AWS outbox](https://aws.amazon.com/blogs/compute/implementing-the-transactional-outbox-pattern-with-amazon-eventbridge-pipes/) y [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests).

Referencias internas: `ARCHITECTURE.md`, `backend/app/agent/platform.py`, `backend/app/db/models.py`, `backend/app/whatsapp/routes.py`, `backend/app/whatsapp/service.py`, `backend/app/jobs/service.py`, `backend/app/exports/service.py` y `backend/app/workbench/`.
