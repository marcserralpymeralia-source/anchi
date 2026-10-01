# BLOQUE 8.1 — Validación HTTPS real controlada

Fecha: 2026-10-01  
Estado: completado, opt-in y cerrado al finalizar.

## Objetivo y endpoint

Se validó por primera vez el camino HTTP real de ANCHI contra el único host autorizado:

```text
https://postman-echo.com
```

Postman Echo se utilizó únicamente como endpoint de testing. No se contactó Sage, Odoo, Meta, ningún ERP, cliente ni API con datos reales.

## Ejecución

La prueba vive separada en `backend/tests/real_network/test_block8_1_postman_echo.py` y solo se ejecuta con ambas variables temporales del proceso:

```text
RUN_REAL_NETWORK_TESTS=true
EXTERNAL_CONNECTOR_HTTP_ENABLED=true
```

La suite normal no abre Internet y omite los cuatro tests de esta carpeta cuando falta el opt-in.

Se utilizó una `IntegrationConnection` sintética en SQLite temporal:

- tenant y connection exclusivos de la validación;
- nombre `Postman Echo Validation`;
- connector lógico `generic_rest`;
- host exacto `postman-echo.com` y puerto 443;
- sin datos GEMAVI, producción, clientes reales ni pedidos reales.

## Resultado

Los cuatro tests opt-in pasaron en 1,413 segundos:

- `GET /get`: HTTP 200; DNS, HTTPS, host exacto, puerto, timeout, tamaño y correlation ID validados.
- `POST /post`: HTTP 200; el JSON ficticio fue devuelto por Echo.
- Generic REST: el request dry-run y el JSON echo recibido fueron equivalentes; `json.externalReference` se mapeó como `external_order_id` de validación de transporte, no como pedido ERP creado.
- `OrderDelivery`: una fixture sintética atravesó delivery → Generic REST → SecureHTTPTransport → respuesta y terminó `exported`.

El cuerpo usado fue exclusivamente sintético: `ANCHI-HTTP-TEST-001`, `TEST-CUSTOMER`, `TEST-SKU` y cantidad `2`.

## SecretProvider y redacción

Se generó un valor ficticio únicamente en memoria del proceso mediante `EnvironmentSecretProvider`. Se inyectó en `X-ANCHI-Test-Credential` solo durante la request POST. Postman Echo lo reflejó en su respuesta; antes de cualquier inspección persistente se aplicó `redact_http_headers`.

La prueba verifica que el valor no aparece en `AuditLog`, eventos, headers seguros ni excepciones. El valor no se incluye en esta documentación, no se versiona y se elimina al terminar el proceso.

## Auditoría y gates

Los eventos persistidos utilizaron el modelo existente `AuditLog`, con:

- tenant y connection sintéticos;
- operation y hostname `postman-echo.com`;
- correlation ID y duración;
- `http.request_attempted` y `http.request_succeeded`;
- sin Authorization, credential ni request body.

También se verificó:

- allowlist exacta: `postman-echo.com` permitido, otro hostname bloqueado antes de DNS/HTTP;
- `https://127.0.0.1` bloqueado antes del request;
- resolver fake para escenarios privados permanece cubierto por los tests offline;
- redirects y timeouts permanecen deterministas y sin ampliar el scope;
- kill switch global y connector gate bloquean sin realizar red.

## Cleanup

Al finalizar:

- `EXTERNAL_CONNECTOR_HTTP_ENABLED` quedó en `false` dentro del proceso de prueba;
- la credencial ficticia fue eliminada del entorno;
- SQLite temporal y datos sintéticos fueron eliminados;
- no se creó configuración versionada ni migración;
- Sage/Odoo siguen bloqueados y no se activó ningún runtime productivo.

## Validación offline y límites

La suite offline continúa siendo la fuente principal para SSRF, DNS rebinding, IPs privadas, redirects, límites, taxonomy y no-retry. La prueba real demuestra conectividad controlada contra un endpoint de echo; no demuestra creación, idempotencia ni contrato de ningún ERP.

Siguiente paso recomendado: revisión de seguridad del diff y, si procede, una validación contractual independiente por connector, manteniendo el gate global cerrado.
