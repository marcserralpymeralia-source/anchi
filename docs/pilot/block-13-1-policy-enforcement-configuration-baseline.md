# Block 13.1 — Policy Enforcement & Configuration Baseline

Fecha: 2026-10-02
Base: `7102385 docs(pilot): approve pilot rbac and retention policy`
Worktree: `modular-order-hub`, rama `feat/modular-order-hub`
Ámbito: enforcement local de RBAC/retention, baseline de configuración y evidencia. Sin push, deploy, producción, credenciales ni conectores reales.

## Resultado ejecutivo

Se implementaron controles locales de mínimo privilegio, validación estricta de confirmación, evaluación tenant-scoped de retención y manifiesto determinista de drift expuesto por readiness. La aprobación humana existente de RBAC y retención no se altera. El gate general sigue **NO GO** porque no hay runtime final ni baseline aprobado configurado.

## A. Pilot profile

Permanece `Catalog-only`: WhatsApp Catalog → Order → Workbench → Generic REST mock → OrderDelivery/Audit. Email/autodiscovery, audio, Sage real y Odoo real permanecen excluidos. El manifiesto marca estos módulos desactivados por defecto; sólo compara contra aprobación si se configura `PILOT_CONFIGURATION_BASELINE_JSON`.

## B. RBAC aplicado

- `Operador`: inbox/workbench y confirmación nominal; la ruta sólo confirma pedidos revisados y sin blockers. Sin integraciones, retry, AuditLog, logs técnicos, administración de jobs ni tenants.
- `Supervisor`: acceso a AuditLog/trazabilidad del tenant y `manage_orders` para retry; sin configuración de integraciones o logs técnicos de plataforma.
- `Administrador`: permisos tenant salvo `view_technical_logs` y `manage_tenants`; conserva `view_logs` para AuditLog tenant. El borrado tenant de AuditLog devuelve 403.
- `Superadmin`: identidad de plataforma con `view_technical_logs` y `manage_tenants`; una identidad de plataforma no puede reutilizarse como usuario tenant. La consola sigue exigiendo `require_superadmin`.

El enforcement es backend, no depende de ocultar UI. La matriz está en `backend/app/core/permissions.py`; el rechazo de identidad cruzada en `backend/app/auth/dependencies.py`.

## C. Confirm-order guards

La confirmación normal falla ante estado pendiente/de revisión, cliente no validado, pedido sin líneas, producto/línea sin validar, cantidad no positiva o cualquier blocker de scoring. Los guards de cliente/producto/cantidad no se desactivan al relajar toggles de scoring. `force-confirm` queda reservado a Administrador/Supervisor y exige motivo.

## D. RetentionPolicyService

`backend/app/core/retention.py` calcula estados `active`, `retained`, `blocked`, `legal_hold` y `eligible_for_deletion`, con dry-run y callback de auditoría para cada apply. Comprueba `tenant_id` en cada registro y evalúa Orders +90 días post-offboarding; datos replicados +90 días; mensajes 90 días desde procesamiento correcto; deliveries/attempts/jobs/errors/AuditLog/correlation IDs 365 días; attachments 30–90 días; temporales/previews 24–72 horas.

- Legal hold tiene precedencia y nunca es elegible para borrado.
- Backups quedan fuera del cleanup tenant: rotación del proveedor a 30 días.
- Secret references permanecen bloqueadas hasta revocación confirmada.
- `PILOT_RETENTION_DESTRUCTIVE_ENABLED=false` por defecto. Para negocio, apply exige además allowlist de categorías; sin ambos controles no ejecuta borrado.
- Temporales/previews se limpian por separado y sólo mediante callback explícito.
- No se instaló scheduler, gestor de secretos ni storage externo. Por eso la política local pasa sus controles, pero G-16 sigue abierto para el piloto.

El modelo master carecía de fecha de offboarding; se añadió `MasterCompany.offboarded_at` y la migración aditiva `2026.10.02.1`, necesarias para medir los 90 días. No se crearon tablas de borrado ni se ejecutó cleanup real.

## E. PilotConfigurationManifest

`backend/app/core/pilot_configuration.py` produce manifiesto canónico y SHA-256 estable: tenant ID, commit/schema/profile, módulos, definiciones de connector, fingerprints de configuración segura, rutas, roles master, gates de runtime y versiones de RBAC/retention. Claves sensibles se sustituyen por `[REDACTED]` antes de serializar/hash; no incluye valores secretos ni PII de conexión.

Readiness expone `configuration_manifest_hash` y estado `MATCH`, `DRIFT`, `NOT_CONFIGURED` o `INVALID_BASELINE`, con diferencias sanitizadas. Profile/módulos/connectors/rutas/runtime gates son críticos. El readiness se hace no-ok ante drift crítico sólo si existe baseline configurado; diffs informativos no bloquean. No hay autocorrección de configuración.

Runtime construye rutas y fingerprints desde la DB del tenant autenticado. No se configuró baseline aprobado en este worktree; estado de runtime: **NOT_CONFIGURED / OPEN**, no MATCH.

## F. Optional-module isolation

Los tres fallos de email autodiscovery y el fallo de WhatsApp audio siguen fuera del perfil y se reprodujeron aislados. El intento ampliado de ejecutar toda `test_regression_flows` reportó además tres fallos email: dos esperan confirmar un `pedido_pendiente_revision` con el nuevo hard guard; el tercero esperaba export/retry, que queda sin confirmar por el mismo guard. Se repitieron individualmente y fallan igual. Son la nueva observación G-26, fuera del contrato Catalog-only; exige que Email registre una transición explícita de revisión antes de habilitarse. No se modificaron autodiscovery, transcripción, Email, Sage ni Odoo.

La selección Catalog-only pilot-critical conserva de `test_regression_flows` únicamente `test_mock_order_uses_tenant_company_name`; los flujos email se prueban en aislamiento y no se cuentan como aceptación del perfil.

## G. Gates externos y decisión

Storage durable, PostgreSQL/backup gestionado, cron/proveedor, alertas externas, browser acceptance final, SecretProvider y destino Generic REST real siguen abiertos. No se ejecutaron operaciones externas ni se usaron credenciales.

**Decisión final: NO GO.** RBAC humano continúa aprobado conforme al registro de Marc en `pilot-rbac-approval.md`; retention continúa aprobada conforme a `pilot-retention-approval.md`. Esta entrega demuestra enforcement/cobertura local, no aprobación de runtime.

La ejecución ampliada previa de 217 tests incluyó todo `test_regression_flows` y terminó con **3 failures** de esos escenarios email. La selección pilot-critical Catalog-only final terminó con **205 tests, 0 failures, 0 errors**. Los cuatro tests residuales explícitos se ejecutaron aparte y reprodujeron **3 fallos email autodiscovery + 1 fallo WhatsApp audio**, ya documentados como excluidos. La batería focused policy/config/closure/migrations/security/delivery terminó con **82 tests, OK**.

## H. Validación

Comandos/resultados definitivos y lista de archivos: [`phase-delivery-block-13-1.md`](../development/phase-delivery-block-13-1.md).
