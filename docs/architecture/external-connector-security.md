# External Connector Security Boundary

Estado: BLOQUE 8, implementado en `feat/modular-order-hub`.

Este documento define el límite común para cualquier HTTP externo futuro de ANCHI. El bloque no habilita conexiones reales a Sage, Odoo ni Generic REST; el gate global `EXTERNAL_CONNECTOR_HTTP_ENABLED` queda cerrado por defecto.

## Principios

- Toda conexión pertenece a un único `company_id` y se resuelve con `tenant_id + connection_id`.
- `IntegrationConnection.config_json` solo contiene configuración no secreta. Las credenciales se referencian mediante `secret://tenant-N/connection-N/name`.
- Los secretos se resuelven tarde, justo antes de construir el request, y nunca se escriben en logs, auditoría, respuestas API o `repr`.
- Los mocks existentes no pasan por HTTP y siguen siendo el único runtime de Sage, Odoo y Generic REST en este bloque.
- La fiabilidad (outbox, idempotencia, reintentos de negocio) permanece fuera del transporte seguro. `SecureHTTPTransport` no reintenta.

## SecretProvider

`backend/app/connectors/security.py` contiene el contrato `SecretProvider`, el proveedor `EnvironmentSecretProvider` para development/test y el seam determinista `MappingSecretProvider` para tests. La referencia debe tener exactamente esta forma:

```text
secret://tenant-12/connection-34/api_key
```

El proveedor comprueba ambas identidades antes de resolver y construye una clave de entorno explícitamente scoped:

```text
ANCHI_SECRET_TENANT_12_CONNECTION_34_API_KEY
```

En producción debe sustituirse el proveedor de entorno por un adaptador de vault/secret manager que conserve el mismo contrato y alcance. No se crea una tabla de secretos ni se copian tokens legacy.

## EgressPolicy y SSRF

`EgressPolicy.bind()` fija por conexión el origen exacto: esquema, host, puerto, path base y direcciones DNS observadas. La política:

- exige HTTPS y puertos 443; HTTP solo se permite explícitamente en `development/test` y puerto 80;
- rechaza userinfo, query/fragment del origin, puertos arbitrarios, localhost, metadata endpoints, loopback, link-local, multicast, unspecified, reserved y redes privadas;
- permite redes privadas únicamente si el host exacto está en `allowed_private_hosts` de una policy local explícita;
- valida que cada request conserve esquema, host, puerto y path base;
- vuelve a resolver DNS antes de enviar y bloquea cambios de conjunto de direcciones (DNS rebinding);
- desactiva redirects en el cliente HTTP y trata cualquier 3xx como bloqueo.

La resolución del sistema se puede reemplazar por un `DNSResolver` falso en tests. Las pruebas no consultan Internet.

## SecureHTTPTransport

Todo futuro adapter HTTP debe recibir un `SecureHTTPTransport` ya construido. La ejecución pasa, en este orden, por:

1. estado activo de la connection;
2. gate del connector/runtime;
3. `EXTERNAL_CONNECTOR_HTTP_ENABLED=true`;
4. `credential_ref` presente y scoped;
5. validación de destino y DNS;
6. límites de URL, headers, request/response y timeouts;
7. inyección tardía de bearer, API key, basic o custom header;
8. request sin redirects ni retries.

Los límites por defecto son conservadores y se pueden ajustar por configuración de transporte. No se aceptan headers de credenciales aportados por el mapping; solo el SecretProvider puede inyectarlos.

## Taxonomía de errores

El transporte normaliza errores en `ExternalConnectorErrorCategory`: `AUTHENTICATION`, `AUTHORIZATION`, `CONFIGURATION`, `VALIDATION`, `RATE_LIMIT`, `TIMEOUT`, `NETWORK`, `REMOTE_TEMPORARY`, `REMOTE_PERMANENT`, `EGRESS_BLOCKED` y `UNKNOWN`. La policy de reintentos pertenece al outbox y no se duplica aquí.

## Auditoría y correlación

Se reutiliza `audit_logs`, ya existente y tenant-scoped; no hay tabla ERP nueva ni migración adicional. `append_integration_audit()` añade eventos sin hacer commit para conservar la transacción del llamador. Se registran:

- `connection.*`: creación, cambios, enable/disable y health check;
- `delivery.*`: queued, started, succeeded/failed;
- `http.*`: intento o bloqueo, destino hostname seguro, status, duración, categoría y correlation id.

Los payloads de pedido y los headers/credenciales no se persisten. `GET /integrations/audit` y `/settings/integrations` exponen únicamente mensajes y metadatos redactados del tenant actual.

## Estado de adapters

`HTTPGenericRESTTransport`, `HTTPOdooJSON2Transport` y `HTTPSage200ESTransport` son seams preparados para una futura habilitación. Ningún factory del registro los selecciona en este bloque. Para habilitar un connector real se exige completar la checklist de [`real-http-enablement-checklist.md`](../integrations/real-http-enablement-checklist.md), revisión de seguridad y un cambio explícito del gate fuera de este bloque.
