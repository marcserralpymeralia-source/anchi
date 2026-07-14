# Aislamiento multi-tenant

## Modelo

- **Master DB**: companias, usuarios, membresias, registro de tenant DB y estado de provisioning.
- **Tenant DB**: datos operativos de la compania activa.
- **Fuente de verdad**: la membresia activa del usuario autenticado en Master DB.

## Flujo de resolucion

```text
request -> session coherente -> membership_id -> user_id -> company_id -> company_slug -> tenant DB -> session SQLAlchemy tenant
```

Reglas:

- `membership_id`, `user_id` y `company_id` deben coincidir.
- `company_slug` y `host` solo verifican coherencia; no seleccionan tenant por si solos.
- una membresia inactiva, una compania inactiva o una tenant DB ausente bloquean el acceso operativo.
- si la sesion es incompleta, el tenant no se resuelve.

## Clasificacion de rutas

- **Publico**: login, health basico.
- **Autenticado sin tenant**: muy pocas rutas, solo si no requieren DB operativa.
- **Master**: diagnostico de plataforma, provisioning y salud master.
- **Tenant**: pedidos, clientes, productos, importacion, learning, alerts, jobs, settings, channels, logs.
- **Master admin**: rutas de plataforma con permiso explicito.

## Reglas de acceso

- El frontend nunca decide el tenant.
- `company_id` enviado por formulario no sustituye al tenant activo.
- una ruta tenant siempre usa la DB del tenant resuelto, no una DB elegida por request.
- el acceso master requiere `Superadmin`.
- un admin de compania no equivale a admin de plataforma.

## Error esperado

- `401` si no hay autenticacion.
- `403` si hay autenticacion pero no permiso.
- `404` si conviene ocultar existencia de un recurso.
- `303` a login si la sesion no es utilizable.
- `503` si la sesion existe pero la tenant DB no esta disponible.

## Ejemplos seguros

- leer un pedido por `order_id` dentro de la tenant DB activa.
- consultar diagnosticos de una compania solo desde Master DB y con permiso master.
- cambiar de compania solo mediante una membresia valida del mismo usuario.

## Checklist para nuevas rutas

- [ ] la ruta sabe si es master o tenant.
- [ ] la ruta no recibe `company_id` para abrir DB.
- [ ] la ruta usa `get_tenant_db` o `get_master_db` segun corresponda.
- [ ] la ruta valida pertenencia antes de leer o escribir.
- [ ] los tests cubren al menos dos companias cuando hay aislamiento.
