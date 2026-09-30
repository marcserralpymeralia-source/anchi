# Plan de POC para Block4B — Sage 200 España

Estado: plan aprobado para discovery/POC; no implementado.
Fecha: 2026-09-30
Conector objetivo: `Sage200ESConnector` condicionado a cliente piloto y contrato confirmado.

## Objetivo

Demostrar, en un tenant no productivo, que ANCHI puede integrar de forma segura el mínimo operativo de Sage 200 España:

1. identificar un cliente;
2. leer un producto y sus datos comerciales disponibles;
3. validar un pedido canónico;
4. crear un pedido de prueba;
5. leer el pedido remoto;
6. reconciliar el resultado después de un timeout o reintento;
7. declarar capabilities y límites sin convertirlos en supuestos globales.

El POC no pretende cubrir todo Sage, sincronización bidireccional completa, catálogo publicado, facturación, producción ni soporte multi-producto.

## No hacer en Block4B

- No crear `SageConnector` genérico.
- No modificar contratos/runtime de producción antes de cerrar los gates.
- No usar datos ni credenciales de producción.
- No conectarse directamente a SQL o a archivos internos de Sage.
- No copiar SDKs o código de GitHub sin revisión de licencia y necesidad justificada.
- No probar la API de Sage 200 UKI para inferir el contrato de España.
- No declarar idempotencia si el tenant no la demuestra.

## Fases

### B4B-0 — Entrada documental

**Entradas:** [checklist de cliente](../integrations/sage-client-discovery-checklist.md), versión/edición, licencia, documentación del tenant y contacto Sage/partner.

**Gates:**

- producto = Sage 200 España;
- edición Advanced;
- licencia Soluciones Conectadas;
- versión mínima confirmada;
- tenant demo/test y empresa de prueba;
- usuario técnico limitado;
- contrato API aplicable;
- método de revocación conocido.

**Salida:** ficha de conexión sin secretos y matriz de capabilities `provisional/confirmed/unsupported`.

### B4B-1 — Descubrimiento de contrato

Registrar únicamente metadatos y ejemplos sintéticos:

- base URL/portal y versión de API;
- auth flow, scopes/roles, expiración y rotación;
- entidades cliente/producto/pedido;
- filtros, paginación y límites;
- errores y códigos;
- request/response redacted;
- ID remoto, número operativo y estado;
- búsqueda por referencia externa;
- eventos/webhooks o polling;
- condiciones de rate limit.

**Salida:** contrato de fixture con ejemplos no sensibles y lista de UNKNOWN restantes.

### B4B-2 — Diseño del adaptador aislado

Antes de conectarlo al runtime, definir el límite de `Sage200ESConnector`:

- configuración por tenant;
- `validate_configuration()` sin llamada destructiva;
- `health` y clasificación de errores;
- lectura de clientes/productos;
- `validate_order()` sin efectos laterales;
- `create_order()` con clave de idempotencia local;
- `find_order_by_idempotency_key()` o equivalente de reconciliación;
- redacción de secretos y payloads sensibles;
- capabilities declaradas solo cuando estén confirmadas.

El adaptador no debe decidir matching, scoring, aprendizaje, revisión humana ni reglas de negocio del dominio canónico.

### B4B-3 — Fixture/mock primero

Crear respuestas controladas para:

- cliente encontrado/no encontrado/ambiguo;
- producto encontrado/inactivo;
- precio ausente o no determinable;
- pedido válido;
- validación de negocio;
- timeout antes de respuesta;
- timeout con pedido ya creado;
- rate limit;
- token expirado;
- error temporal;
- error permanente.

El primer test debe verificar que la política de delivery de ANCHI no duplica un pedido cuando el commit remoto es desconocido.

### B4B-4 — Sandbox/tenant de prueba

Ejecutar el mínimo de casos con datos sintéticos:

| Caso | Resultado requerido |
|---|---|
| GET cliente | mapping estable y empresa correcta |
| GET producto | código, unidad y disponibilidad/price mode explícitos |
| validate order | errores deterministas, cero efectos laterales |
| create order | ID/número remoto y estado inicial |
| read order | mismo pedido y líneas equivalentes |
| replay exacto | replay idempotente o detección previa, nunca duplicado silencioso |
| timeout desconocido | consulta de reconciliación antes de reintentar |
| error permanente | no retry, auditoría redacted |
| token expirado | refresh/re-auth controlado, sin perder la outbox |

### B4B-5 — Decisión de salida

Clasificar cada capability:

- `confirmed`: contrato y prueba positiva en tenant de prueba;
- `provisional`: documentación/partner, sin prueba suficiente;
- `unsupported`: no disponible o no autorizado;
- `unknown`: pendiente, no se permite usar en producción.

Solo una capability `confirmed` puede habilitar una implementación posterior. La salida real del POC debe incluir riesgos, límites, coste de operación y recomendación de implementación.

## Contrato de prueba mínimo

### Datos de entrada canónicos

- `company_id` ANCHI y `connector_key` aislados;
- cliente externo conocido;
- una línea de producto;
- cantidad positiva;
- moneda/impuesto si Sage los exige;
- referencia externa determinista;
- payload sintético conservado para auditoría.

### Datos que no se deben enviar

- datos de producción;
- credenciales en `raw_payload`;
- tokens/cookies;
- contraseñas;
- secretos de cliente;
- información no necesaria para el pedido.

## Criterios de aceptación

1. El producto/edición/versión se identifica sin ambigüedad.
2. El tenant de prueba tiene licencia y permisos confirmados.
3. El adaptador distingue Sage 200 ES de Active y UKI.
4. Las capabilities no demostradas permanecen `unknown`.
5. Existe mapping estable de cliente/producto y scope de empresa.
6. Se puede validar sin efectos laterales.
7. La creación devuelve identificador remoto y estado.
8. La repetición no genera duplicado silencioso.
9. Un timeout con commit desconocido se reconcilia o queda en estado manual, nunca se reenvía ciegamente.
10. Los errores, logs y fixtures no contienen secretos ni datos de producción.
11. El POC puede desactivar/revocar la conexión sin tocar runtime global.
12. La decisión de implementación queda respaldada por contrato, pruebas y coste operativo.

## Resultado alternativo si el cliente es Sage Active

Si el primer cliente no es Sage 200 sino Sage Active ES/PT, detener este plan antes de B4B-2 y abrir una variante `SageActiveESConnector` con:

- GraphQL ES/PT;
- OAuth 2.0 y scopes;
- `X-OrganizationId`/`LegislationCode`;
- queries/mutations de customers/products/sales orders;
- prueba específica del cálculo de precio y stock;
- misma política ANCHI de outbox, idempotencia y reconciliación.

No se deben mezclar DTOs, endpoints ni auth de ambas variantes.

## Artefactos que deben quedar al cerrar Block4B

- ficha de producto/versión/licencia;
- contrato redacted y ejemplos sintéticos;
- matriz de capabilities;
- fixtures/mock y pruebas de error;
- tabla de mappings externos;
- resultado de idempotencia/reconciliación;
- checklist de revocación;
- decisión `GO`, `GO CONDICIONADO` o `NO-GO`;
- recomendación de implementación del siguiente bloque.
