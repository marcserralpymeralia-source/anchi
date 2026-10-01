# Odoo 19+ JSON-2 — contract verification

Estado: BLOQUE 6 PoC local. No hay HTTP real ni contrato de una base Odoo cliente verificado.

La integración elige el identifier estable `odoo` y lo documenta como Odoo 19+ JSON-2. La documentación oficial confirma el transporte `POST /json/2/<model>/<method>`, bearer API key, `X-Odoo-Database` opcional y documentación dinámica en `/doc`. Los modelos y campos concretos de una base deben verificarse allí.

Fuente primaria: [External JSON-2 API — Odoo 19.0](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html).

| Operation | Officially verified | Mocked | Runtime enabled |
|---|---:|---:|---:|
| `customers.read` / `res.partner` | transporte y modelo conceptual; fields tenant-specific pendientes | Sí | No |
| `products.read` / `product.product` | transporte y modelo conceptual; fields/variants pendientes | Sí | No |
| `pricing.read` / pricelists | No; depende de módulos, reglas y `/doc` | Sí, `source_price != authoritative_price` | No |
| `inventory.read` | No verificado | No | No |
| `orders.create` / `sale.order` | método genérico JSON-2; payload de venta pendiente | Sí | No |
| `orders.read` / `sale.order` | método/modelo conceptual; fields pendientes | Sí, vía reconciliación mock | No |
| order confirmation / `action_confirm` | No habilitado; semántica pendiente | No; el mock deja `draft` | No |
| reconciliation | No existe todavía una garantía tenant-specific | Sí, por referencia mock | No |

## Decisiones del PoC

- `OdooConnector → OdooClient → OdooTransport` mantiene separado el protocolo, la lógica de negocio y el transporte.
- `MockOdooJSON2Transport` simula clientes, variantes `product.product`, pricing, creación, errores y reconciliación.
- El mapper usa un DTO marcado `UNVERIFIED_ODOO_CONTRACT`; no presenta los fields de venta como contrato verificado.
- `client_order_ref` se usa únicamente como candidato de idempotencia del fixture. Debe verificarse en `/doc` o sustituirse por un campo/módulo custom antes de activar HTTP real.
- Los pedidos se crean con semántica `draft`; no se llama a `action_confirm` automáticamente.
- `ExternalIdentity` es obligatorio para cliente y variante de producto. No se busca por nombre ni teléfono durante exportación.
- `real_http_enabled=true` siempre se rechaza. Las capabilities mock solo se ofrecen en `development/test`.

## Pendiente para 4C/activación real

1. Obtener `/doc` de una base cliente.
2. Verificar modelos, campos, comandos de líneas, pricing, permisos y multi-company.
3. Confirmar la estrategia de idempotencia y reconciliación.
4. Implementar `HTTPOdooJSON2Transport` sobre el `httpx` existente, con credencial resuelta por SecretProvider.
5. Sustituir `UNVERIFIED_ODOO_CONTRACT` por un contrato versionado y habilitar capabilities una por una.
