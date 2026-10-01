# Entrega de fase: BLOQUE 8 — External Connector Security & Runtime Boundary

## 1. Objetivo

Crear una frontera común y cerrada por defecto para futuros HTTP externos de ANCHI, con SecretProvider, egress policy, transporte seguro, taxonomía, auditoría y estados visibles.

## 2. Alcance ejecutado

Se implementó el límite tenant/connection scoped, resolución tardía de secretos, protección SSRF/DNS rebinding, redirects desactivados, límites, no-retry, redacción central, auditoría en `AuditLog`, endpoint/UI de estado y seams para Generic REST/Odoo/Sage.

## 3. Alcance no ejecutado

No se habilitó HTTP real, no se accedió a Internet, no se modificaron credenciales legacy, no se añadió un ERP específico, no se creó una tabla nueva ni se modificó producción.

## 4. Diagnóstico previo

Existían `IntegrationConnection`, `OrderDelivery`, `AuditLog` y el registro de connectors. `AuditLog` ya era tenant-scoped y se reutiliza. El transporte Generic REST era un bloqueo explícito; Sage/Odoo eran mocks.

## 5. Cambios realizados

- `security.py`: SecretProvider, EgressPolicy, SecureHTTPTransport, gates y taxonomy.
- `observability.py`: redacción de headers y auth schemes.
- `integrations/audit.py`: append/list/serialization sin commit interno.
- lifecycle de integrations y delivery: eventos persistentes correlacionables.
- Generic REST, Odoo y Sage: seams HTTP únicamente mediante SecureHTTPTransport.
- API/UI: `GET /integrations/audit`, estado de credenciales y estado del gate externo.
- tests de amenaza y documentación operativa.

## 6. Archivos modificados

Los archivos de aplicación y tests se enumeran en el diff final; no se modificaron modelos ni migraciones.

## 7. Archivos creados

`backend/app/connectors/security.py`, `backend/app/integrations/audit.py`, `backend/tests/test_connector_security.py`, `docs/architecture/external-connector-security.md` y `docs/integrations/real-http-enablement-checklist.md`.

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativa descartada |
|---|---|---|
| Reutilizar `AuditLog` | Ya es tenant-scoped y soporta mensajes estructurados | Tabla de actividad ERP nueva |
| SecretProvider por referencia | Evita secretos raw en `config_json` | Copiar credenciales a `IntegrationConnection` |
| Resolver DNS y comparar direcciones | Mitiga SSRF y rebinding | Confiar solo en hostname |
| Un transporte común | Evita bypasses por adapter | HTTP directo en cada connector |
| Gate global false | Reduce riesgo de activación accidental | Habilitar por defecto en development/production |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest backend/tests/test_connector_security.py` | 37 tests OK |
| Regresión Generic REST + registry + delivery + Odoo + Sage + observabilidad | 102 tests OK |
| AST de módulos modificados | OK |

## 10. Tests añadidos o modificados

`test_connector_security.py` cubre SecretProvider, tenant/connection isolation, redaction, SSRF, metadata/local/private IPs, mixed DNS, rebinding, HTTPS/ports/path, gates, late injection, raw headers, redirects, limits, taxonomy, timeout, no-retry, audit y tenant scoping.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| HTTP global cerrado por defecto | Cumplido | `SecureHTTPTransport.global_gate_enabled()` y tests |
| Ningún HTTP real para Sage/Odoo/Generic | Cumplido | factories mock-only y adapters gated |
| SSRF/DNS rebinding/redirects | Cumplido | EgressPolicy y tests fake |
| Auditoría correlacionable y redactada | Cumplido | `AuditLog`, `correlation_id`, endpoint y tests |
| Sin migración nueva | Cumplido | reutilización de modelos existentes |

## 12. Riesgos y observaciones pendientes

El proveedor de entorno es solo para development/test; debe reemplazarse por un vault aprobado antes de cualquier HTTP real. La validación de contrato, allowlist concreta y activación de cada ERP queda para una fase posterior.

## 13. Desviaciones

La auditoría se implementó sobre el modelo existente en vez de crear una tabla nueva, conforme al objetivo de minimizar esquema.

## 14. Estado final de Git

Pendiente de commits locales del bloque; no se hará push ni se configurará upstream.

## 15. Recomendación siguiente

Revisión de seguridad y verificación contractual controlada de un único connector, manteniendo el gate global cerrado.
