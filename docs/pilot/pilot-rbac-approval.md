# Aprobación humana de RBAC del piloto

Estado: **PENDING HUMAN APPROVAL**

Fecha de propuesta: 2026-10-01  
Revisión técnica Block 13: 2026-10-01  
Ámbito: primer piloto Catalog-only de ANCHI-GEMAVI  
Datos usados en esta validación: únicamente sintéticos

## Matriz propuesta

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

La matriz se basa en `DEFAULT_ROLE_PERMISSIONS` y en la dependencia de permisos de las rutas. La tabla es una propuesta de mínimo privilegio y debe ser confirmada por el owner del piloto y Seguridad.

## Revisión técnica Block 13

- `Administrador` y `Supervisor` son los únicos roles tenant autorizados para recovery manual de entregas fallidas.
- `Operador` no tiene `manage_tenants`, `manage_users`, `configure_integrations` ni la ruta de offboarding.
- `Superadmin` es necesario para gestionar tenants/offboarding; la ruta exige actor de plataforma, motivo y CSRF.
- Las pruebas de aislamiento, permisos y offboarding pasan dentro de la suite pilot-critical.
- No se ha revisado ninguna cuenta humana real, MFA, caducidad ni lista de soporte porque no existe un entorno final autorizado.

Esta revisión técnica no equivale a aprobación. El documento permanece **PENDING HUMAN APPROVAL**.

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

## Aprobaciones requeridas

| Rol | Nombre | Decisión | Fecha | Firma/enlace |
|---|---|---|---|---|
| Seguridad | ____________________ | PENDING HUMAN APPROVAL | __________ | ____________________ |
| Owner del piloto | ____________________ | PENDING HUMAN APPROVAL | __________ | ____________________ |
| Responsable de plataforma | ____________________ | PENDING HUMAN APPROVAL | __________ | ____________________ |

## Condición de salida

No se invitan usuarios reales ni se declara GO PILOT hasta que esta matriz, el procedimiento de alta/baja y la revisión de cuentas privilegiadas estén aprobados y asociados al entorno/commit desplegado.
