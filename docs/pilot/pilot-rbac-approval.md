# Aprobación humana de RBAC del piloto

Estado: **APPROVED FOR PILOT**

Fecha de propuesta: 2026-10-01  
Revisión técnica Block 13: 2026-10-01  
Ámbito: primer piloto Catalog-only de ANCHI-GEMAVI  
Datos usados en esta validación: únicamente sintéticos

## Decisión del owner

| Pilot Owner | Decision | Date |
|---|---|---|
| Marc Serral | APPROVED | 2026-10-01 |

Esta es la única aprobación humana registrada en este documento. No se inventan aprobadores ni firmas de terceros.

## Matriz aprobada

| Capacidad | Operador | Supervisor | Administrador | Superadmin |
|---|---:|---:|---:|---:|
| Ver inbox/workbench y pedidos | Sí | Sí | Sí | Sólo contexto de plataforma |
| Gestionar conversación y responder WhatsApp | Sí | Sí | Sí | No por defecto |
| Editar pedido / cliente / producto | No | Sí | Sí | No por defecto |
| Confirmar pedido | Sí | Sí | Sí | No por defecto |
| Exportar pedido / recuperar entrega fallida | No | Sí | Sí | No por defecto |
| Configurar canales, agente y scoring | No | No | Sí | No por defecto |
| Configurar integraciones y activar/pausar conexiones | No | No | Sí | No por defecto |
| Gestionar usuarios del tenant | No | No | Sí | No por defecto |
| Gestionar jobs técnicos del tenant | No | No | Sí | No por defecto |
| Gestionar tenants y ejecutar offboarding | No | No | No | Sí |
| Ver logs técnicos de plataforma | No | No | No | Sí, sólo necesidad operativa |

La matriz se basa en `DEFAULT_ROLE_PERMISSIONS` y en la dependencia de permisos de las rutas. No amplía permisos adicionales.

## Revisión técnica Block 13

- `Administrador` y `Supervisor` son los únicos roles tenant autorizados para recovery manual de entregas fallidas.
- `Operador` puede confirmar únicamente pedidos que hayan superado las validaciones requeridas y estén en estado apto para confirmación/exportación; la ruta normal bloquea blockers y revisión pendiente.
- `Supervisor` y `Administrador` pueden ejecutar recovery manual de entregas fallidas según el backend actual.
- `Administrador` puede consultar el AuditLog/trazabilidad de su tenant.
- `Supervisor` dispone de la visibilidad tenant de trazabilidad necesaria para diagnosticar deliveries recuperables, sin acceso a logs técnicos de plataforma.
- Los logs técnicos de plataforma permanecen reservados a `Superadmin` como decisión operativa del piloto.
- `Operador` no tiene `manage_tenants`, `manage_users`, `configure_integrations` ni la ruta de offboarding.
- `Superadmin` es necesario para gestionar tenants/offboarding; la ruta exige actor de plataforma, motivo y CSRF.
- La autorización debe seguir aplicándose en backend; ocultar acciones en UI no sustituye RBAC.
- Las pruebas de aislamiento, permisos y offboarding pasan dentro de la suite pilot-critical.
- Las cuentas de soporte serán nominativas. MFA, caducidad y revisión de cuentas se validarán en el runtime final.

### Discrepancia técnica abierta

`DEFAULT_ROLE_PERMISSIONS["Administrador"]` se construye con todas las claves de `PERMISSIONS`, incluida `view_technical_logs`, mientras que la cadena de permisos de plataforma `Superadmin` no contiene esa clave. Las rutas de plataforma usan además `require_superadmin`, pero no existe una separación backend/documental limpia del permiso técnico. Esto no coincide con la decisión aprobada de reservar los logs técnicos de plataforma a `Superadmin`. No se modifica código en este encargo; el gate técnico RBAC permanece **OPEN** hasta resolver o aceptar explícitamente esta discrepancia.

## Controles de offboarding y soporte

- Operador y Supervisor no pueden ejecutar offboarding ni administrar tenants.
- Administrador puede operar el tenant, pero no puede cambiar el estado de la compañía desde superadmin.
- Superadmin requiere autenticación de plataforma, motivo y confirmación CSRF para offboarding.
- Offboarding desactiva membresías y canales, impide nuevos jobs/deliveries y mantiene la evidencia histórica.
- Las cuentas de soporte deben ser nominativas; MFA, caducidad y revisión periódica quedan sujetos a la política de identidad aprobada.

## Evidencia técnica disponible

- Suite crítica: 207 tests, 0 failures, 0 errors.
- Aislamiento multitenant y denegaciones por rol cubiertos por `test_multitenancy_auth`, `test_security_config` y `test_connector_security`.
- Browser sintético mostró el botón y el diálogo de offboarding; el efecto final se verificó mediante el servicio y la base sintética porque el driver nativo quedó bloqueado al confirmar el diálogo.

## Registro de aprobación

| Rol | Nombre | Decisión | Fecha | Firma/enlace |
|---|---|---|---|---|
| Pilot Owner | Marc Serral | APPROVED | 2026-10-01 | Decisión humana registrada |

## Condición de salida

La aprobación humana está concedida para el alcance Catalog-only, pero no cierra el gate global: la discrepancia técnica de logs, la validación de cuentas en runtime final y los demás gates de runtime permanecen abiertos.
