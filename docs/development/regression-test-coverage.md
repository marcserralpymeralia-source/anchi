# Cobertura de regresion

## Flujos cubiertos

| Flujo | Test | Resultado esperado |
|---|---|---|
| Email -> mensaje entrante -> conversacion -> procesamiento -> pedido -> revision -> aprobacion -> exportacion simulada -> auditoria | `backend/tests/test_regression_flows.py::RegressionFlowsTests::test_email_to_review_confirm_export_and_audit_flow` | Crea conversacion, pedido, review, job de exportacion, export file y trazas de auditoria |
| Correo duplicado -> no duplica mensaje -> fallo de procesamiento -> retry -> pedido valido -> fallo de exportacion -> retry -> exito | `backend/tests/test_regression_flows.py::RegressionFlowsTests::test_duplicate_mail_retry_and_export_retry_path` | Mantiene un solo mensaje, recupera el procesamiento y completa exportacion tras reintento |
| Dedupe de mensajes y conversaciones | `backend/tests/test_messages_and_conversations.py` | Mantiene un solo inbound message y una sola conversacion para el mismo identificador |
| Fiabilidad de jobs y reintentos | `backend/tests/test_jobs_reliability.py` | Valida idempotencia, historial de intentos, recovery y worker |
| Estado operativo del pedido | `backend/tests/test_order_state.py` | Garantiza que scoring, bloqueo y confirmacion sigan coherentes |
| Aislamiento multi-tenant | `backend/tests/test_tenant_isolation.py` | Bloquea acceso cruzado entre companias |
| Migraciones y compatibilidad | `backend/tests/test_schema_migrations.py` | Verifica ledger, inventario y simulacion de bases existentes |

## Notas de cobertura

- Los tests nuevos no llaman a proveedores reales.
- La exportacion se simula con `FTPService.send` parcheado.
- El fallo y el retry de exportacion se validan con un error retryable y una reejecucion posterior.
- La cobertura estructural sigue apoyandose en las suites existentes para tenant, jobs, migraciones y mensajes.
