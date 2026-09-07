# Entrega de fase

## 1. Objetivo

Preparar un módulo de configuración por tenant para conectar una base de datos externa de forma segura, inspeccionar su esquema y permitir que el administrador vincule campos de clientes y productos con el maestro local de Anchi.

## 2. Alcance ejecutado

- Alta y edición de fuentes externas PostgreSQL y MySQL/MariaDB, con SQLite limitado a demo, desarrollo y pruebas.
- Credenciales almacenadas cifradas; la interfaz y los resúmenes nunca devuelven la contraseña.
- Conexión de prueba con consulta de solo lectura y timeouts de conexión.
- Escaneo acotado del esquema, ordenado de forma determinista y con aviso cuando el resultado está limitado.
- Persistencia del último esquema válido para que la interfaz no pierda la información al cerrar y volver a abrir el módulo.
- Mapeo configurable de campos de clientes y productos, validando columnas existentes, campos obligatorios y columnas binarias.
- Previsualización limitada de filas sin guardar datos externos.
- Sincronización manual, limitada y auditable hacia los maestros locales de clientes y productos.
- Actualización por código de cliente o referencia de producto sin crear duplicados cuando cambia el nombre.
- Dedupe por identificador dentro de cada lote y tratamiento de errores parciales por fila.
- Aislamiento por tenant, autorización de administrador/superadministrador y propiedad validada en todas las operaciones.
- Migración incremental para instalaciones que ya tenían creada la tabla de conexiones.

## 3. Alcance no ejecutado

- No se ha conectado ninguna base de datos real de una empresa ni se han usado datos de producción.
- El campo de proxy queda preparado en la configuración, pero el túnel de base de datos por proxy no está habilitado todavía; el gateway existente solo ofrece salud.
- No se ha añadido sincronización automática programada. La sincronización es manual y explícita para mantener el control durante esta fase.
- No se ha ejecutado un smoke test contra PostgreSQL o MySQL reales porque no hay credenciales ni endpoint de prueba autorizado en este entorno.

## 4. Diagnóstico previo

Existía la estructura inicial de fuentes y mapeos, pero faltaban persistencia del esquema escaneado, controles de entorno para SQLite, restauración de la interfaz, sincronización robusta de maestros y pruebas de actualización/aislamiento.

## 5. Cambios realizados

La conexión se configura desde Ajustes → Fuentes de datos. El flujo recomendado es:

1. Crear la fuente y guardarla.
2. Ejecutar “Probar conexión”.
3. Ejecutar “Escanear esquema”.
4. Seleccionar una tabla, mapear sus campos y guardar el mapeo.
5. Activar el mapeo y ejecutar una previsualización o sincronización manual.

La fuente externa siempre se consulta en modo de solo lectura desde Anchi. Los registros válidos se incorporan mediante los servicios de maestro existentes, sin crear un pipeline alternativo.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/db/models.py` | Guardar el último esquema seguro | Modelo |
| `backend/app/external_databases/service.py` | Validación, escaneo, lecturas y sincronización | Lógica de dominio |
| `backend/app/migrations/registry.py` | Añadir la columna a instalaciones existentes | Migración |
| `backend/app/settings/routes.py` | Persistencia, autorización y precondiciones de operaciones | Backend web |
| `backend/app/templates/settings/index.html` | Restauración del esquema y formularios por motor | Interfaz |
| `backend/app/templates/whatsapp/_chat_live.html` | Ajuste pendiente de interfaz del buzón | Interfaz |
| `backend/app/whatsapp/inbox_routes.py` | Ajuste pendiente de payload del buzón | Backend web |
| `backend/tests/test_external_databases.py` | Cubrir producción, restauración y sincronización de productos | Tests |
| `backend/tests/test_schema_migrations.py` | Cubrir reparación de instalaciones existentes | Tests |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/development/external-database-sources-delivery.md` | Registro de alcance, decisiones y evidencias de esta fase |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Configuración por tenant | Evita mezclar credenciales y datos de empresas | Configuración global compartida |
| Contraseña cifrada | Permite conectar sin exponer secretos en HTML o logs | Guardarla en claro |
| SQLite solo en demo/pruebas | Evita tratar una ruta local como fuente productiva | Permitir SQLite en Vercel o producción |
| Escaneo y preview acotados | Protege tiempo, memoria y respuesta web | Cargar un esquema o tabla sin límites |
| Sincronización manual en esta fase | Evita trabajos largos no controlados en despliegues serverless | Lanzar sincronizaciones automáticas todavía |
| Código/referencia como identidad | Impide duplicados al cambiar nombres descriptivos | Identificar por nombre |
| Proxy preparado pero no operativo | El túnel aún no existe en el gateway | Simular una conexión que no estaría soportada |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python -m compileall -q app tests` | Correcto |
| `python -m unittest tests.test_external_databases` | 4 tests OK |
| `python -m unittest tests.test_schema_migrations` | 23 tests OK |
| `python -m unittest tests.test_external_databases tests.test_schema_migrations` | 27 tests OK |
| `python -m unittest discover -s tests` | Correcto, exit code 0 |
| Validación de los bloques JavaScript embebidos con Node | 2 scripts OK |
| `git diff --check` | Sin errores de whitespace |

Los comandos Python se ejecutaron desde `backend` usando el entorno virtual del proyecto. El smoke test de PostgreSQL permanece condicionado a sus variables de entorno y no se ejecutó contra ningún servidor real.

## 10. Tests añadidos o modificados

- SQLite no se acepta en producción ni cuando el proceso corre en Vercel.
- El esquema escaneado se puede recuperar al volver a abrir Ajustes.
- La sincronización parcial informa errores y conserva su estado real.
- La sincronización de productos crea y después actualiza por referencia sin duplicar.
- La migración añade la columna de snapshot a una tabla de conexiones preexistente.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Configuración aislada por tenant | Cumplido | Validaciones de propiedad en rutas y servicio |
| Secretos protegidos | Cumplido | Cifrado existente y resúmenes sin contraseña |
| Verificación y escaneo de fuente | Cumplido | Tests de flujo SQLite y límites de esquema |
| Mapeo campo a campo | Cumplido | Validación de destino/origen y campos obligatorios |
| Alimentación de clientes y productos | Cumplido para demo/pruebas | Tests de alta y actualización por identidad |
| No modificar la fuente externa | Cumplido por diseño | Consultas SELECT y transacciones de solo lectura |
| Conexión real de empresa | Pendiente | Requiere endpoint y credenciales autorizadas |
| Proxy de base de datos | Pendiente | Requiere implementar el túnel en el gateway |

## 12. Riesgos y observaciones pendientes

- Para producción se deben usar usuarios externos con permisos de solo lectura y certificados/CA apropiados.
- PostgreSQL y MySQL deben probarse con una base de datos de integración antes de activar una fuente real.
- Las columnas no incluidas en el snapshot por los límites del escaneo no podrán mapearse hasta ampliar o volver a ejecutar el escaneo con una estrategia adecuada.
- La sincronización manual debe convertirse en un job controlado cuando se defina la política de frecuencia y reintentos.

## 13. Desviaciones respecto al alcance inicial

Se ha mantenido el proxy como configuración disponible, pero no se ha simulado ni implementado el túnel de datos porque el gateway actual no lo ofrece. También se ha dejado la sincronización manual para evitar introducir trabajos largos en el entorno serverless antes de definir el scheduler.

## 14. Estado final de Git

La entrega se prepara sobre la rama por defecto del repositorio. Tras la validación, los cambios de esta fase quedan listos para commit y push; no se incluyen secretos, bases de datos locales, logs ni artefactos de pruebas.

## 15. Recomendación para la siguiente fase

Probar con una base de integración PostgreSQL y otra MySQL/MariaDB, validar permisos de solo lectura, definir el contrato del túnel proxy y después añadir sincronización asíncrona con lock por tenant, reintentos y métricas de duración/filas.
