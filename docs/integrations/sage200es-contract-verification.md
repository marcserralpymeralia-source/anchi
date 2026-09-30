# Sage 200 ES — contract verification report

Fecha: 2026-09-30
Producto objetivo: Sage 200 España / Soluciones Conectadas
Estado: PoC local con transporte mock; HTTP real bloqueado.

## Regla de evidencia

La documentación pública de Sage confirma la existencia y los prerrequisitos de Soluciones Conectadas, pero no publica en las fuentes consultadas un contrato completo y versionado de entidades, rutas, payloads, errores o idempotencia. Por eso las operaciones mock no se presentan como API Sage implementada.

`UNVERIFIED_SAGE_CONTRACT` identifica toda forma provisional del PoC.

## Matriz de operaciones

| Operation | Officially verified | Mocked | Runtime enabled |
|---|---:|---:|---:|
| `sites.read` | NO | SÍ | NO |
| `products.read` | NO | SÍ | NO |
| `customers.read` | NO | SÍ | NO |
| `rates.read` | NO | SÍ | NO |
| `price_offers.read` | NO | SÍ | NO |
| `inventory.read` | NO | NO | NO |
| `orders.create` | NO | SÍ | NO |
| `orders.read` | NO | SÍ | NO |
| `order reconciliation` | NO | SÍ | NO |

## Evidencia oficial utilizada

- [Sage 200 España — Soluciones Conectadas](https://sage200c.sage.es/help/Content/Conect/Sage-soluciones-conectadas.htm): API de intercambio de datos de ventas, licencia, Advanced, versión mínima, activación administrativa, Sage ID, usuario de aplicación y alcance por empresas.
- [Sage 200 España — esquema funcional](https://sage200c.sage.es/help/Content/Introduccion/esquema-funcional.htm): Plataforma Connect y orientación a integraciones de terceros.
- [Sage 200 España — declaración oficial de producto](https://www.sage.com/es-es/-/media/files/sagedotcom/spain/documents/pdf/certificados/declaracion_responsable_sage200_30-4-25.pdf): modalidades local/online/Azure y conectividad general, sin convertirla en contrato REST.

## Qué puede cambiar el estado a YES

Para cada operación hace falta documentación de desarrollador aplicable al producto/versión o una prueba autorizada en tenant no productivo que permita registrar:

- endpoint y versión;
- request/response redacted;
- permisos y scope de empresa/site;
- paginación y límites;
- errores y semántica retryable;
- identificador remoto;
- comportamiento ante repetición y timeout;
- mecanismo de reconciliación.

Hasta entonces, ninguna operación de esta tabla puede habilitar HTTP real.

## Fixtures

Los datos de `backend/tests/fixtures/sage200es_mock_contract.json` son sintéticos y conceptuales. Incluyen `source_documentation`, `source_url` y `contract_status`; no son respuestas oficiales de Sage ni contienen datos de cliente.
