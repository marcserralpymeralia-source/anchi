# Informe de readiness para piloto

Fecha: 2026-10-01

Versión auditada: commit Block 9 `3bac724` más cambios y evidencia de Block 11
Resultado: **NO GO condicionado**

## Resumen ejecutivo

El camino más seguro para un primer piloto es WhatsApp Catalog estructurado → un único destino REST/ERP, con revisión humana para excepciones. La lógica de deduplicación, aislamiento de tenant, outbox, backoff, reconciliación y roles tiene evidencia automatizada positiva.

No se recomienda abrir todavía un piloto con adjuntos, email autodiscovery/backfill, audio/transcripción o múltiples conectores. Block 11 añade readiness de storage y recuperación manual de entregas, pero siguen sin demostrarse storage persistente tras reinicio, backup/restore, cron/alertas y offboarding ejecutable.

## Evidencia ejecutada

| Evidencia | Resultado |
|---|---|
| Suite enfocada Block 9 | 56/56 OK |
| `test_multitenancy_auth.py` aislado | 4/4 OK |
| `test_schema_migrations.py` | 24/24 OK |
| `test_order_delivery_reliability.py` | 13/13 OK |
| `test_whatsapp_catalog_inbound.py` | 10/10 OK |
| Storage + reliability + observability focalizados | 21/21 y 9/9 OK |
| Fresh deployment browser smoke | Login, pedidos, workbench y entregas fallidas visibles en servidor sintético |
| Smoke sintético de volumen | 100 pedidos → 100 deliveries → 100 jobs |
| Full suite | 673 tests; 10 failures, 2 errors, 1 skipped |

El full suite y cada fallo están clasificados en [pilot-gap-register.md](./pilot-gap-register.md). Los cuatro fallos de multitenancy se reprodujeron como 4/4 OK al ejecutar el módulo aislado; no deben ocultarse, pero apuntan a contaminación de estado/orden de suite.

## Contrato propuesto de primer piloto

Incluido:

- un tenant o un grupo pequeño de tenants explícitamente aprobados;
- WhatsApp Catalog estructurado, con `catalog_id`, productos y clientes mapeados;
- destino único con healthcheck y contrato de idempotencia;
- revisión humana en workbench cuando identidad, producto, cantidad o unidad no sean concluyentes;
- exportación/entrega asíncrona con worker dedicado;
- soporte en horario definido y runbook.

Excluido:

- email autodiscovery y backfill IMAP con adjuntos;
- audio, transcripción y conversación libre;
- múltiples destinos o routing complejo;
- billing, marketplace, self-service onboarding y nuevos conectores;
- datos de producción no anonimizados.

## Evaluación por dimensión

| Dimensión | Estado | Lectura |
|---|---|---|
| Funcionalidad Catalog | Verde condicionado | 10/10; deduplicación, mappings, review y outbox cubiertos |
| Entrega | Verde condicionado | 13/13; retries, reconciliación, tenant scope y concurrencia cubiertos |
| Multitenancy/RBAC | Ámbar | tests aislados OK; falta cerrar determinismo de full suite y matriz firmada |
| Storage | Ámbar condicionado | readiness real implementado; persistencia final y reinicio aún no demostrados |
| DB/migraciones | Ámbar | 24/24 en copias/SQLite; falta ejecución real y restore de PostgreSQL |
| Workers/cron | Ámbar | lógica presente; faltan alerta, proveedor y prueba operativa |
| Email | Rojo para inclusión | fallos de backfill/autoconfig; excluir del primer contrato |
| WhatsApp audio | Rojo para inclusión | descarga/transcripción no pasa; excluir |
| Observabilidad | Ámbar | IDs/audit útiles; métricas no durables y no hay timeline unificado |
| PII/offboarding | Rojo | no hay política ejecutable verificada |
| UI/browser | Ámbar | smoke local completado; falta repetir en runtime y navegador finales |

## Gates de go-live

Los gates obligatorios están en [pilot-go-live-checklist.md](./pilot-go-live-checklist.md). En síntesis, deben cerrarse: storage persistente y su healthcheck en staging, backup/restore, migraciones del entorno real, worker/cron con alertas, procedimiento confirmado para confirmación/exportación, aceptación staging de DLQ, RBAC/offboarding, browser smoke final y un full suite determinista o una excepción formal con casos excluidos.

## Simulación operativa

La simulación de volumen creó 100 eventos sintéticos en una base SQLite temporal y observó 100 pedidos, 100 outbox y 100 jobs. La concurrencia de claim y las transiciones de entrega están cubiertas por los 13 tests de reliability; no es un benchmark de producción ni sustituye una prueba de carga con PostgreSQL.

Trace mínimo recomendado para la prueba real:

```text
webhook/external_event_id
  -> inbound_message_id
  -> order_id
  -> delivery_id + idempotency_key
  -> background_job_id
  -> attempt_id
  -> audit/correlation_id
  -> external_order_id
```

## Riesgo residual aceptable sólo bajo exclusión

Email, audio, autodiscovery y adjuntos pueden permanecer como P2 si el contrato escrito de piloto los excluye, las UI no los anuncian como disponibles y el runbook explica cómo conservar y escalar el evento. Storage no puede quedar bajo esa excepción si se reciben documentos.

## Decisión

**No abrir tráfico real todavía.** Re-evaluar cuando los gates P1 estén cerrados y exista una prueba de extremo a extremo en el entorno que se va a entregar. Si se decide un piloto aún más reducido, debe ser Catalog-only, sin adjuntos/audio/email, con datos sintéticos y un único destino mock/REST controlado.
