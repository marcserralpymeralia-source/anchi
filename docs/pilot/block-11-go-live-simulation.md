# Block 11 — Go-live simulation

## Escenarios ejecutados

| Escenario | Resultado |
|---|---|
| 100 pedidos sintéticos | 100 orders, 100 deliveries y 100 jobs |
| Retry/recovery de delivery | Test OK; mantiene idempotency key y no duplica delivery |
| Aislamiento tenant | Tests de claim, jobs y rutas de datos OK |
| RBAC de operación | Retry restringido a Administrador/Supervisor; suspensión revoca sesión |
| Browser smoke | Login, navegación, pedido de prueba, workbench y recuperación visibles |
| Migraciones | 24/24 tests OK |

## Escenarios no aprobados

- restore real de master, tenant y objetos;
- caída/recuperación del worker con alerta externa;
- ejecución del cron del proveedor con secreto y timeout;
- persistencia de adjuntos tras reinicio/redeploy;
- offboarding con retención, borrado/anonimización y backups;
- full suite completamente determinista.

## Decisión

La simulación demuestra que la lógica de recuperación y el camino local son operables, pero no satisface los gates de infraestructura y gobierno necesarios para tráfico real. Resultado: **NO GO**.
