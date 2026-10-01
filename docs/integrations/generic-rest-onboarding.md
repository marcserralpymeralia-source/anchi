# Generic REST onboarding

Esta guía configura una API REST minoritaria sin guardar secretos ni modificar código.

## 1. Crear la connection

En `Settings → Integrations → Generic REST` o en `POST /integrations/connections`, enviar:

```json
{
  "connector_key": "generic_rest",
  "name": "Acme ERP",
  "secret_provider_ref": "erp/acme/prod",
  "credentials_configured": true,
  "config": {
    "base_url": "https://erp.example/api",
    "auth": {"type": "bearer"},
    "operations": {}
  }
}
```

`secret_provider_ref` es solo una referencia. El JSON de `config` no puede contener `token`, `password`, `api_key`, `Authorization` ni valores equivalentes.

## 2. Definir las operations

Una API tipo Acme puede configurarse así:

```json
{
  "customers.read": {
    "method": "GET",
    "path": "/customers/{customer_id}",
    "path_params": {"customer_id": "order.customer.external_id"},
    "response_mapping": {"external_id": "code"}
  },
  "products.read": {
    "method": "GET",
    "path": "/products/{sku}",
    "path_params": {"sku": "line.product.external_id"},
    "response_mapping": {"external_id": "sku"}
  },
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
    "response_mapping": {"external_order_id": "orderNumber", "status": "status"},
    "idempotency": {"location": "header", "name": "Idempotency-Key"}
  },
  "orders.read": {
    "method": "GET",
    "path": "/orders/by-reference/{reference}",
    "path_params": {"reference": "order.idempotency_key"},
    "response_mapping": {"external_order_id": "orderNumber"}
  }
}
```

Una API con otro contrato puede cambiar únicamente esta configuración:

```json
{
  "orders.create": {
    "method": "POST",
    "path": "/sales-orders",
    "request_mapping": {
      "client": "order.customer.external_id",
      "externalRef": "order.idempotency_key",
      "items": {"source": "order.lines", "items": {"sku": "line.product.external_id", "qty": "line.quantity"}}
    },
    "response_mapping": {"external_order_id": "salesOrderNo"},
    "idempotency": {"location": "body", "name": "externalRef"}
  }
}
```

## 3. Validar y activar

Guardar una configuración inválida la deja `MISCONFIGURED`; no entra en routing. La activación valida:

- base URL y controles SSRF;
- operaciones permitidas y método/path relativo;
- paths fuente y mappings de arrays;
- `orders.create` con `external_order_id` de respuesta;
- status codes, idempotencia y límites;
- credenciales fuera de `config_json`.

El registry muestra las capabilities derivadas de esa connection. Para el fixture local, `mock_scenario` puede ser `acme_a`, `acme_b`, `acme_a_timeout_after_create`, `acme_a_rate_limited` o `acme_a_invalid_payload`.

## 4. ExternalIdentity

Crear identidades de cliente y producto con el namespace exacto `generic_rest:<connection_id>`. No usar `generic_rest` a secas si el tenant tiene más de una API. La entrega rechaza una identidad de otro tenant o de otra connection.

## 5. Dry-run y routing

Antes de activar una ruta, ejecutar:

```http
POST /integrations/connections/{connection_id}/dry-run
Content-Type: application/json

{"order_id": 123}
```

El resultado muestra el request que se enviaría, sin transporte HTTP. Después, seleccionar la connection activa con `PUT /integrations/routes/order`; el backend exige `orders.create`.

## 6. Reconciliation

`OrderDelivery` consulta `orders.read` antes de crear y vuelve a consultar tras un resultado remoto desconocido. Si el ERP no ofrece idempotency key ni búsqueda por referencia, la configuración no debe anunciar garantías exactly-once: se requiere una política de reconciliación específica.

## 7. No soportado

No introducir URLs desde el pedido, headers inbound, scripting, `eval`, secretos en mappings, DELETE, XML/SOAP, CSV/multipart, GraphQL o sincronización de stock implícita. HTTP real queda bloqueado en BLOQUE 7.
