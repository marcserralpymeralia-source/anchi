# Checklist para habilitar HTTP real de un connector

Esta lista es una condición previa; no queda satisfecha por el trabajo del BLOQUE 8. Sage 200 ES, Odoo y Generic REST continúan en mock-only y `EXTERNAL_CONNECTOR_HTTP_ENABLED` permanece `false`.

## Contrato y configuración

- [ ] El contrato externo está verificado para una conexión concreta y versionado en documentación.
- [ ] El connector declara capabilities runtime reales, no `mock_capabilities`.
- [ ] La factory real está separada del mock y solo se carga de forma lazy por `connection_id`.
- [ ] `config_json` no contiene tokens, contraseñas, cookies, `Authorization`, API keys ni material equivalente.
- [ ] La conexión usa un `secret://tenant-N/connection-N/name` válido y el SecretProvider de producción está aprobado.
- [ ] La credencial se rota/revoca sin escribir secretos en base de datos ni en logs.

## Egress y red

- [ ] `EgressPolicy` fija el host exacto, HTTPS, puerto autorizado y path base.
- [ ] La allowlist de hosts privados, si existe, está justificada por conexión y no usa rangos amplios.
- [ ] DNS se resuelve con el resolver aprobado y se prueba rebinding/mixed answers.
- [ ] SSRF a localhost, metadata, loopback, link-local, private y reserved está cubierto por tests.
- [ ] Redirects están desactivados y no existe follow-up a un host alternativo.
- [ ] El cliente usa `trust_env=False` salvo una decisión revisada para proxy controlado.

## Transporte y límites

- [ ] El adapter solo usa `SecureHTTPTransport`; no usa `requests`, `httpx` ni sockets directamente.
- [ ] No hay retries dentro del adapter; se delega la fiabilidad al outbox/idempotencia.
- [ ] Timeouts connect/read/total, URL, headers, request bytes y response bytes están definidos.
- [ ] 401/403/408/429/5xx y errores de red se mapean a la taxonomía común.
- [ ] Se valida JSON/schema antes de incorporar datos remotos al dominio.
- [ ] No se almacenan cuerpos de pedido ni respuestas completas en auditoría.

## Gates y operación

- [ ] La connection está `ACTIVE` y tiene credenciales configuradas.
- [ ] El connector/runtime gate está explícitamente habilitado para esa implementación.
- [ ] `EXTERNAL_CONNECTOR_HTTP_ENABLED` se habilita solo mediante cambio revisado y reversible.
- [ ] Hay kill switch documentado para volver a `false` sin migración.
- [ ] Health check y dry-run no se interpretan como autorización para enviar pedidos reales.
- [ ] Existe observabilidad con `request_id`, `correlation_id`, tenant, connection, operation, hostname, status, duración y categoría.
- [ ] Los eventos de conexión, delivery y HTTP aparecen en `audit_logs` sin secretos y respetan tenant isolation.

## Verificación

- [ ] Tests con DNS y transporte fake cubren SSRF, rebinding, redirects, límites, gates, redacción, no-retry y aislamiento.
- [ ] Tests de contrato usan fixtures grabadas o servidor local controlado; no consultan Internet desde CI.
- [ ] Se ejecuta la regresión de Generic REST, Odoo, Sage, registry, outbox y observabilidad.
- [ ] Se revisan diff, migraciones, logs y un rollback operativo antes de cambiar el gate.
- [ ] La aprobación de seguridad identifica explícitamente el tenant, connector, host y periodo de habilitación.
