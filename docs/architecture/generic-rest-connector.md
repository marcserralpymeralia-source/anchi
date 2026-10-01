# Generic REST Connector

Estado: BLOQUE 7, PoC local mock-only.

`GenericRESTConnector` cubre la cola larga de ERPs minoritarios sin crear un adapter por cliente. No es un proxy HTTP, un motor ETL ni un runtime de código: cada `IntegrationConnection` contiene únicamente operaciones JSON declarativas que el backend valida antes de activar.

## Capas

```text
GenericRESTConnector
        ↓
GenericRESTClient
        ↓
MockGenericRESTTransport  (BLOQUE 7)
HTTPGenericRESTTransport  (seam bloqueado)
```

El connector reutiliza `OrderDelivery`, `OrderDeliveryAttempt`, retries y reconciliation existentes. No tiene una reliability layer propia.

## Configuration schema

La configuración vive en `IntegrationConnection.config_json`. No se guardan tokens, passwords ni valores de headers de credenciales:

```json
{
  "base_url": "https://erp.example/api",
  "auth": {"type": "bearer"},
  "timeout_seconds": 10,
  "max_request_bytes": 262144,
  "max_response_bytes": 1048576,
  "max_lines": 500,
  "real_http_enabled": false,
  "mock_scenario": "acme_a",
  "operations": {
    "orders.create": {
      "method": "POST",
      "path": "/orders",
      "request_mapping": {
        "customerCode": "order.customer.external_id",
        "externalReference": "order.idempotency_key",
        "lines": {
          "source": "order.lines",
          "items": {
            "articleCode": "line.product.external_id",
            "quantity": {"source": "line.quantity", "type": "number"}
          }
        }
      },
      "response_mapping": {
        "external_order_id": "orderNumber",
        "status": "status"
      },
      "idempotency": {"location": "header", "name": "Idempotency-Key"}
    }
  }
}
```

Supported operations are `customers.read`, `products.read`, `pricing.read`, `inventory.read`, `orders.create`, `orders.read` and `orders.find_by_reference` (the last two expose `orders.read`). Methods are `GET`, `POST`, `PUT` and `PATCH`; DELETE and non-JSON protocols are outside this block.

Capabilities are derived from the validated operation keys of that connection. A connection with only `orders.create` never declares `inventory.read` or any other global capability.

## Mapping DSL

The evaluator supports:

- allow-listed field paths such as `order.customer.external_id` and `line.quantity`;
- output rename through mapping keys;
- arrays through `source: order.lines` plus `items`;
- `{ "constant": "ACME" }`;
- `{ "optional": true, "default": "" }`;
- scalar conversion to `string`, `integer`, `number` or `boolean`.

The validator rejects unknown paths, duplicate list targets, expressions, imports, shell fragments, template syntax, `eval`, arbitrary keys and mappings that introduce a host or protocol. Response mappings are simple JSON paths; they can expose `external_order_id`, `status`, `authoritative_price` or other explicitly named neutral outputs.

## URL and SSRF security

`base_url` is administrative connection configuration. Order data cannot choose the host. Operation paths must be relative, start with `/`, contain no query/fragment, traversal, scheme, hostname or unresolved dynamic URL. Path placeholders are substituted only from allow-listed order fields and reject absolute URL/path-like values.

The validator rejects loopback, localhost, metadata, link-local, reserved and private IP targets unless a precise private-host allowlist is supplied. Production will require HTTPS. Static request headers use an allowlist and cannot carry `Authorization`, cookies, proxy/forwarded headers, host headers or secret values. Auth is represented by a scheme (`bearer`, `api_key`, `basic` or `custom_header`) and a future SecretProvider reference.

Requests and responses are bounded to 256 KiB and 1 MiB by default; timeout is capped at 30 seconds and line count at 500. Bodies are not placed in the safe audit record. The audit record contains connection, operation, request id, result category, status and duration; persistence into the worker audit stream is a later operational concern.

## Identities, pricing and inventory

Outbound customer/product mapping requires an `ExternalIdentity` belonging to the same tenant and connection namespace:

```text
generic_rest:<integration_connection_id>
```

This avoids collisions between two generic APIs in one tenant. Generic REST does not perform fuzzy matching by name or phone.

`pricing.read` may return `authoritative_price`; the source price received by ANCHI remains distinct. No pricing formula is evaluated in the DSL. `inventory.read` is optional and never becomes an implicit precondition for `orders.create`; stock policy belongs to tenant rules.

## Dry-run and mock contract

`GenericRESTConnector.dry_run_order()` returns method, relative path, query, safe headers and mapped JSON without invoking a transport. The authenticated API endpoint is `POST /integrations/connections/{id}/dry-run` with a tenant-owned `order_id`.

`MockGenericRESTTransport` provides two fixtures:

- AcmeERP A: `/customers/{code}`, `/products/{sku}`, `/orders`, `/orders/by-reference/{reference}` and `orderNumber`.
- ERP B: `/clients/{code}`, `/items/{sku}`, `/sales-orders`, `/sales-orders/by-ref/{reference}` and `salesOrderNo`.

Both use the same connector and differ only in `IntegrationConnection` operations/mappings. No `AcmeERPConnector` exists.

## Explicit limitations

- HTTP real is blocked, including the prepared `HTTPGenericRESTTransport`.
- No XML, SOAP, CSV, multipart or GraphQL.
- No arbitrary request URLs, inbound host selection, scripts, code evaluation or template engine.
- Pagination is only a validated declaration (`none`, `page`, `cursor`); no generic pagination engine is implemented.
- No profile marketplace/versioning or OAuth flow.
- No promise of exactly-once when a destination lacks idempotency or reconciliation.
