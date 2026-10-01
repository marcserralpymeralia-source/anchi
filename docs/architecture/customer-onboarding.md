# Customer Onboarding & Activation

## Objetivo

El onboarding de un cliente configura y valida una ruta completa `inbound → order routing → outbound` sin crear un wizard persistido ni depender de SQL manual. El estado visible se deriva de la configuración real del tenant y de sus eventos de prueba.

## Modelo y fuente de verdad

El flujo reutiliza las entidades existentes:

- `IntegrationConnection`: connector registrado, configuración no secreta, referencia a SecretProvider, estado y health.
- `TenantIntegrationRole`: roles de master data y destino de pedidos.
- `OrderRoute`: origen, destino, tipo de pedido y activación del routing.
- `ExternalIdentity`: mappings de customers/products por tenant y sistema externo.
- `AuditLog`: inicio, cambios, dry-run, pruebas, activación y pausa.

No se introduce una tabla de onboarding ni una migración en este bloque. `IntegrationReadinessService` calcula `NOT_STARTED`, `BLOCKED`, `READY_FOR_TEST`, `READY_FOR_ACTIVATION` o `ACTIVE` y devuelve blockers/warnings accionables.

## Flujo de activación

1. Seleccionar connector inbound y outbound desde `ConnectorRegistry`.
2. Crear connections en `CONFIGURED`; las credenciales solo se referencian mediante `secret_provider_ref`.
3. Asignar `TenantIntegrationRole` y `OrderRoute` en modo configuración.
4. Resolver mappings locales desde la API tenant-scoped de candidates y guardarlos como `ExternalIdentity`.
5. Ejecutar `Test connection` y `Dry-run`. El dry-run genera el payload pero devuelve explícitamente `external_request: NOT_SENT`.
6. Confirmar `Activar integración`. Se reevalúa readiness y se habilitan origen, destino y route dentro de un savepoint transaccional; si un paso falla, no queda activado parcialmente.
7. `Pausar integración` desactiva route y connections, conservando configuración, roles y mappings para reanudar.

## Registry y límites de contrato

- `FakeInbound → FakeERP`: escenario estable para desarrollo y pruebas, sin tráfico externo.
- `Generic REST`: mock-only durante este bloque; admite mapping y generación de request con contrato configurable, pero muestra warning de habilitación productiva pendiente.
- `Sage 200 ES` y `Odoo`: pueden configurarse para preparar el onboarding, pero readiness permanece bloqueado hasta completar verificación contractual. No se envía HTTP real.

La política global de HTTP externo continúa siendo la autoridad de seguridad. Los secretos no se aceptan en `config_json` y los eventos pasan por la redacción de auditoría existente.

## API principal

- `GET /integrations/onboarding/readiness`
- `GET /integrations/onboarding/connectors`
- `POST /integrations/onboarding/start`
- `PUT /integrations/onboarding/roles/{role_key}`
- `PUT /integrations/onboarding/route`
- `GET /integrations/onboarding/mappings/{connection_id}`
- `GET /integrations/onboarding/mappings/{connection_id}/candidates`
- `PUT /integrations/onboarding/mappings`
- `POST /integrations/onboarding/test/{connection_id}`
- `POST /integrations/onboarding/dry-run`
- `POST /integrations/onboarding/activate`
- `POST /integrations/onboarding/pause`

Las operaciones de lectura están tenant-scoped. Las mutaciones pasan por la autorización existente de integraciones; la activación/pausa usa la capacidad separada `activate_integrations` y la configuración usa `configure_integrations`.
