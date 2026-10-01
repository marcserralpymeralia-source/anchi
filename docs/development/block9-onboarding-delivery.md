# Bloque 9 — Customer Onboarding & Activation

## 1. Objetivo

Entregar onboarding guiado y activación segura de integraciones por tenant, reutilizando el registry y los modelos de integración existentes.

## 2. Alcance ejecutado

- Readiness derivado, sin tabla wizard ni migración.
- UI `/settings/onboarding` y API para connectors, connections, roles, route, mappings, test, dry-run, activate y pause.
- Candidate search tenant-scoped para customers/products.
- Activación atómica y pausa/reanudación conservando configuración.
- Audit trail y redacción existentes ampliados a eventos de onboarding.
- RBAC separado entre `configure_integrations` y `activate_integrations`.
- Escenario FakeInbound → FakeERP y Generic REST mock preparado.

## 3. Alcance no ejecutado

Sage 200 ES y Odoo no tienen contrato real verificado y permanecen bloqueados. No se añade HTTP real, secretos, migración de base de datos, ni procesamiento de producción.

## 4. Diagnóstico previo

La plataforma ya disponía de `IntegrationConnection`, `TenantIntegrationRole`, `OrderRoute`, `ExternalIdentity`, `AuditLog` y `ConnectorRegistry`. Se decidió derivar onboarding sobre esas entidades para evitar dos fuentes de verdad.

## 5. Cambios realizados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/integrations/readiness.py` | Calcular estado y blockers | Creado |
| `backend/app/integrations/onboarding.py` | Orquestar configuración, pruebas y ciclo de vida | Creado |
| `backend/app/integrations/routes.py` | Exponer API y página | Modificado |
| `backend/app/integrations/service.py` | Permitir configuración previa a activación y exponer dirección | Modificado |
| `backend/app/integrations/audit.py` | Auditar eventos del onboarding | Modificado |
| `backend/app/core/permissions.py` | Separar capacidades RBAC | Modificado |
| `backend/app/templates/settings/onboarding.html` | UI guiada | Creado |
| `backend/tests/test_onboarding.py` | Cobertura de servicio, tenant isolation y atomicidad | Creado |

## 6. Decisiones técnicas

- Se reutilizan modelos existentes; no hay `OnboardingSession` persistida.
- Las conexiones se crean `CONFIGURED` y solo se activan tras readiness + acknowledge explícito.
- El dry-run no llama a HTTP y deja evidencia `NOT_SENT`.
- Sage/Odoo se muestran como configurables pero contractualmente bloqueados.

## 7. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest -v backend/tests/test_onboarding.py` | 15/15 OK |
| Regresión completa | 673 tests: 661 OK, 10 fallos y 2 errores preexistentes del entorno; la batería focalizada de 56 tests pasa |

## 8. Tests añadidos o modificados

Se cubren tenant vacío, source/destination, credenciales, capabilities, route/roles, mappings Generic REST, Sage/Odoo bloqueados, audit, tenant isolation, dry-run, test connection, activación con acknowledge, atomicidad y pause/resume.

## 9. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| UI/API sin SQL manual | Cumplido | Página y endpoints de onboarding |
| Readiness accionable | Cumplido | `IntegrationReadinessService` |
| Activación atómica | Cumplido | savepoint + test de rollback |
| Pausa/reanudación | Cumplido | servicio y prueba dedicada |
| Mappings con búsqueda | Cumplido | endpoint candidates + selector UI |
| No credenciales en config/audit | Cumplido | reutiliza validation/redaction existentes |
| Sage/Odoo sin HTTP real | Cumplido | contract blocker |

## 10. Riesgos y siguientes pasos

Queda pendiente la verificación contractual y un adapter productivo para Sage/Odoo, además de decidir el contrato productivo de cada Generic REST tenant. Antes de producción conviene añadir pruebas de endpoints con autenticación real y una comprobación e2e visual de la página.

## 11. Recomendación de commit

`feat(onboarding): add tenant integration activation flow`
