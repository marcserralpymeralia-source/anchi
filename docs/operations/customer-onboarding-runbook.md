# Runbook: alta y activación de un cliente

## Precondiciones

1. Tener usuario del tenant con permisos de configuración y activación de integraciones.
2. Confirmar que el entorno es `development`, `test` o el entorno productivo autorizado para el connector elegido.
3. Tener disponibles únicamente referencias de SecretProvider; no pegar tokens, passwords ni claves en el formulario.
4. Para un primer smoke test, usar `FakeInbound → FakeERP`.

## Procedimiento normal

1. Abrir `/settings/onboarding`.
2. Pulsar **Iniciar onboarding**.
3. Crear las connections de entrada y destino desde las opciones del registry. Revisar estado, credentials y capabilities.
4. Guardar el destino en el rol `order_destination` y guardar el routing source → destination.
5. Para `Generic REST`, buscar y seleccionar los customers/products locales y registrar sus external IDs.
6. Ejecutar **Test connection**. Debe devolver `SUCCESS`; `FAILED` requiere corregir config/credenciales y repetirlo. `BLOCKED` requiere una decisión de contrato o política, no un reintento ciego.
7. Ejecutar **Dry-run** y comprobar que el payload sea el esperado y que indique `NOT_SENT`.
8. Revisar todos los blockers. Solo cuando readiness sea `READY_FOR_ACTIVATION`, pulsar **Activar integración** y confirmar.
9. Verificar estado `ACTIVE`, route activa y el evento `onboarding.activated` en auditoría.

## Pausar y reanudar

Para detener nuevos envíos, pulsar **Pausar integración**. La route queda inactiva y las connections `DISABLED`, pero se conservan configuración, roles y mappings. Para reanudar, ejecutar de nuevo el test/dry-run si procede y activar con confirmación; no recrear connections ni duplicar mappings.

## Diagnóstico de blockers

| Código/estado | Acción |
|---|---|
| `source_missing` / `destination_missing` | Crear y seleccionar connections del sentido correcto. |
| `credentials_missing` | Configurar la referencia de SecretProvider; nunca guardar el secreto en JSON. |
| `configuration_invalid` | Corregir el schema del connector y repetir Test connection. |
| `role_missing` / `role_capability_missing` | Asignar el rol a una connection que declare la capability requerida. |
| `route_missing` | Guardar source → destination para `order`. |
| `identity_unresolved` | Completar ExternalIdentity para cada customer/product local exigido por Generic REST. |
| `dry_run_required` | Ejecutar un dry-run local y revisar el payload antes de activar. |
| `connection_test_required` | Repetir Test connection hasta obtener `SUCCESS`. |
| `contract_unverified` | Sage/Odoo permanecen bloqueados; completar contrato y habilitación en un bloque posterior. |
| `mock_only` | Es un warning esperado en Generic REST; no implica tráfico real. |

## Evidencias y seguridad

La auditoría registra inicio, cambios de route/roles/mappings, test, dry-run, activación y pausa. Los mensajes se redactan mediante el boundary común de observabilidad. En este bloque no se deben activar Sage/Odoo ni habilitar HTTP real de Generic REST.

## Rollback operativo

Si la activación falla, revisar el response con blockers y el audit log. La operación es atómica: no debería quedar solo una connection activa. Corregir la causa, repetir Test connection/Dry-run y activar de nuevo. Si el tenant ya está activo y se requiere detenerlo, usar Pause; no borrar conexiones.
