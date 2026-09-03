# Auditoría de rendimiento de Anchi Gemavi

## 1. Objetivo

Identificar los cuellos de botella que están provocando la lentitud de la demo publicada en `https://anchi-gemavi.vercel.app/` y dejar un plan priorizado de actuación. Este documento comenzó como análisis y plan de actuación; tras la revisión del usuario, la sección 5 registra las optimizaciones aplicadas en el checkout local. No se ha desplegado ni publicado ningún cambio.

Fecha de medición: 02/09/2026.

## 2. Alcance ejecutado

- Acceso como usuario normal mediante la cuenta demo proporcionada.
- Navegación de solo lectura por portada, pedidos, bandeja, clientes, productos, configuración e importación manual.
- Medición de peticiones autenticadas repetidas para comprobar si la lentitud era puntual.
- Medición de HTML generado, tamaño transferido con Brotli, cabeceras de caché y recursos estáticos.
- Revisión estática del código local para relacionar los síntomas con rutas, consultas y plantillas.
- No se crearon, editaron ni eliminaron clientes, productos, pedidos, mensajes o configuraciones.

## 3. Alcance no ejecutado

- No se han hecho cambios de base de datos, Vercel ni configuración de producción.
- No se ha implementado todavía la paginación SQL completa de pedidos/bandeja, el shell diferido de Configuración ni la carga bajo demanda de formularios ocultos.
- No se han ejecutado acciones POST de negocio; el único POST fue el login necesario para la prueba.
- No se han medido Core Web Vitals reales, CPU/layout del navegador ni waterfall completo porque el navegador integrado no estaba disponible en esta sesión.
- La medición es externa y orientativa: el tiempo indicado incluye conexión, ejecución serverless y descarga.

## 4. Diagnóstico previo

### 4.1 Resultado visible

Las respuestas dinámicas son consistentemente lentas incluso al repetir la misma pantalla. El problema dominante es el tiempo de servidor/generación, no el tamaño final de transferencia.

| Pantalla | Tiempo observado | HTML sin comprimir | HTML transferido con Brotli |
|---|---:|---:|---:|
| Login | 2,4–5,1 s | — | 12,0 KB en una ejecución |
| Pedidos (`/orders`) | 3,1–4,1 s | 150,3 KB | 27,7 KB |
| Bandeja (`/entries`) | 3,4–3,8 s | 111,3 KB | 12,0 KB |
| Clientes (`/customers`) | 5,1–5,5 s | 192,2 KB | 19,9 KB |
| Productos (`/products`) | 2,0–2,4 s | 205,9 KB | 15,6 KB |
| Configuración (`/settings`) | 8,9–9,0 s | 116,2 KB | 23,1 KB |
| Importación manual | 2,5–2,8 s | 39,5 KB | — |

En todas las peticiones dinámicas autenticadas se observó:

- `x-vercel-cache: MISS`.
- `Cache-Control: public, must-revalidate, max-age=0`.
- No había una respuesta reutilizable de HTML entre navegaciones.

La compresión sí está activa cuando el cliente la solicita: la hoja `styles.css` pesa 279,8 KB sin comprimir y se entregó comprimida en aproximadamente 42,7 KB. Por tanto, reducir el trabajo del backend debe tener prioridad sobre seguir reduciendo unos pocos KB de red.

### 4.2 Hallazgos críticos P0

#### P0-1. Configuración construye una pantalla monolítica y repite trabajo

`backend/app/settings/routes.py:249` (`settings_page`) prepara de una vez todas las secciones, aunque el usuario solo vaya a consultar una de ellas:

- Calcula métricas de agente y sugerencias de mejora cargando correos, pedidos, líneas y relaciones.
- Construye diagnósticos de entorno con numerosos `COUNT` independientes.
- Carga módulos de empresa, identidad, canales, IA, scoring, decisión, exportación, FTP, usuarios y alertas.
- Obtiene versiones de prompts con una consulta por plantilla (`prompt_versions`), patrón N+1.
- Vuelve a cargar ajustes y conteos desde `build_settings_dashboard` (`:301`) y `build_environment_diagnostics` (`:377`).

La página tarda alrededor de 9 segundos de forma estable. Es el primer objetivo de rendimiento.

**Instrucción urgente:** servir primero un shell ligero de configuración y cargar cada drawer/sección bajo demanda. Consolidar las lecturas de ajustes en una sola carga por modelo, sustituir el N+1 de versiones por una consulta agrupada y convertir métricas/diagnósticos en agregados SQL o endpoints diferidos.

#### P0-2. Pedidos carga todos los registros antes de paginar y repite la consulta base

`backend/app/dashboard/service.py:566` (`load_order_view_data`) ejecuta `.all()` sobre todos los pedidos que cumplen los filtros, carga conversaciones, líneas y relaciones, y solo después calcula los índices de la página.

Además, `backend/app/orders/routes.py:203-216` llama de nuevo a `load_order_view_data` sin el estado seleccionado para construir los contadores de pestañas. Esto vuelve a cargar el conjunto completo para una información que debería resolverse con `COUNT/GROUP BY`.

**Instrucción urgente:** aplicar `LIMIT/OFFSET` o paginación por cursor en la consulta principal; ejecutar los contadores de estados con una única agregación SQL; cargar líneas, conversaciones y adjuntos únicamente para las filas visibles; evitar que las vistas tarjetas y lista materialicen dos veces el mismo dataset.

#### P0-3. Bandeja y workbench procesan datasets completos en Python

`backend/app/pages/routes.py:380-448` llama a `workbench_summary` para la bandeja y a `load_order_view_data` más `orders_workbench_summary` para pedidos en tarjetas. `workbench_summary` obtiene todos los pedidos y correos del intervalo, crea objetos de presentación y filtra después en Python.

Esto escala con el histórico del tenant aunque el usuario solo vea 25 filas. También explica que una actualización o cambio de filtro vuelva a pagar el coste completo.

**Instrucción urgente:** separar tres operaciones: `count/summary`, página de IDs y carga detallada de la página. Mover filtros, ordenación y clasificación que puedan expresarse en SQL al gestor de consultas; reservar el cálculo Python para las 25–100 filas visibles.

#### P0-4. Clientes carga simultáneamente lista y conocimiento

`backend/app/customers/routes.py:196` siempre ejecuta `build_databases_context` y `customer_knowledge_overview`, incluso cuando `view=list`. El segundo (`backend/app/databases/service.py:168`) hace varias agregaciones, carga todos los clientes con aliases, dominios, contactos y puntos de contacto, y recorre el resultado completo.

La vista de conocimiento necesita esa información, pero la lista normal no. La duplicación es un coste fijo alto y crecerá con los datos.

**Instrucción urgente:** no calcular conocimiento en la vista list; crear una carga separada y paginada para `view=knowledge`; cargar el detalle del cliente solo cuando se seleccione uno.

### 4.3 Hallazgos importantes P1

#### P1-1. Se descarga JavaScript duplicado en pedidos, clientes y productos

`backend/app/templates/base.html:10` incluye `/static/js/database-tables.js?v=8` globalmente. A la vez, las plantillas de pedidos, clientes y productos vuelven a incluirlo mediante `asset_url` (`orders/list.html:6`, `customers/list.html:4`, `products/list.html:4`).

En producción se observaron dos URLs distintas con el mismo contenido de aproximadamente 22,9 KB:

- `/static/js/database-tables.js?v=8`
- `/static/js/database-tables.js?v=155f2dd73a1a0000-5991`

Aunque cada recurso está cacheado, las claves distintas obligan al navegador a descargar y ejecutar dos veces el mismo archivo en esas páginas.

**Instrucción:** decidir si el script es global o específico de tablas, dejar una sola inclusión y mantener una única URL versionada.

#### P1-2. HTML inicial demasiado grande por formularios ocultos

Productos entrega aproximadamente 206 KB de HTML y clientes 192 KB. Las plantillas contienen formularios completos de edición/borrado para cada fila (`products/list.html:327-360`, y equivalentes en clientes), aunque el usuario no los haya abierto. El recuento observado fue de 56 formularios en productos y 31 en clientes.

**Instrucción:** renderizar una sola plantilla de editor en un drawer/modal y pedir el detalle al abrirlo; mantener en la tabla solo los datos visibles y acciones ligeras. No eliminar capacidades: diferir su carga.

#### P1-3. Hoja CSS global monolítica

`backend/app/static/styles.css` pesa 279,8 KB sin comprimir y se entrega en todas las pantallas. Brotli/gzip reduce el coste de red, pero el navegador sigue teniendo que descargar, parsear y aplicar una hoja grande en cada primera visita.

**Instrucción:** separar un `core.css` pequeño de hojas por área (`orders`, `customers`, `settings`, `imports`), medir el impacto real con cobertura CSS y eliminar reglas duplicadas. No dividir a ciegas: conservar el CSS común de navegación y temas.

#### P1-4. Versionado de CSS inconsistente

La URL observada de la hoja global tiene dos signos de interrogación (`styles.css?v=...?...`) porque `asset_url` ya añade una versión y `base.html:9` añade otra. Funciona como clave de caché, pero es frágil y genera URLs innecesariamente complejas.

**Instrucción:** usar una sola estrategia de versionado basada en hash de contenido, sin concatenar un segundo `?v=` manual.

#### P1-5. Falta de diagnóstico de rendimiento en producción

El código local tiene instrumentación `X-Perf-*` en `backend/app/core/performance.py`, pero las respuestas de producción no devolvieron esas cabeceras. No se puede saber desde fuera si cada segundo se consume en conexión a base de datos, consultas, Python o Jinja.

**Instrucción:** activar métricas internas muestreadas y no sensibles para rutas críticas: duración total, SQL acumulado, número de consultas, duplicados, renderizado, registros cargados y tamaño de respuesta. Enviar los datos a logs/observabilidad, no al HTML público, y añadir un `request_id` correlacionable.

#### P1-6. Revisar región y coste de arranque serverless

Las rutas pasan por Vercel y abren/usan conexiones a las bases master y tenant. `backend/app/tenancy/database.py:18-24` mantiene engines cacheados por proceso, pero en instancias frías el coste de conexión y de comprobación de esquema reaparece. `get_tenant_db` también invoca la comprobación de esquema una vez por proceso.

**Instrucción:** medir por separado cold start, conexión master, conexión tenant y primera consulta; ejecutar migraciones fuera de las peticiones; revisar que Vercel y la base de datos estén en regiones compatibles; ajustar pool/pre-ping solo después de medir.

### 4.4 Lo que no parece ser el cuello principal

- La compresión HTTP funciona y reduce mucho los bytes transferidos.
- Los recursos estáticos tienen `Cache-Control: public, max-age=31536000, immutable`.
- El CSS y JavaScript versionados permiten caché; el problema actual es la duplicación y el tamaño inicial, no la ausencia total de caché.
- No hay evidencia suficiente para culpar a un único proveedor externo sin instrumentar el servidor.

## 5. Cambios realizados

- Se eliminó la doble inclusión de `database-tables.js` en las listas de pedidos, clientes y productos. El script queda cargado una sola vez desde la plantilla base mediante `asset_url`.
- Se corrigió el versionado de `styles.css`: se eliminó el `?v=` añadido manualmente después de una URL que ya incorpora hash.
- La vista de clientes en modo lista limita el resumen de conocimiento a los IDs de la página actual. La vista de conocimiento conserva su carga completa porque necesita filtrar y ordenar ese conjunto.
- Las versiones de prompts de Configuración se obtienen en una única consulta agrupada, conservando una lista por plantilla.
- Configuración reutiliza en dashboard y diagnóstico la misma compañía y los mismos ajustes ya cargados en la petición, eliminando lecturas repetidas.
- `agent_metrics` usa agregados SQL para correos, pedidos y líneas en lugar de hidratar todos los objetos del periodo.
- Pedidos evita la segunda materialización completa del dataset cuando no hay filtro de estado; los contadores se calculan sobre el dataset canónico ya cargado.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/templates/base.html` | Versionado único y carga única del script común | Plantilla |
| `backend/app/templates/orders/list.html` | Eliminar recurso JavaScript duplicado | Plantilla |
| `backend/app/templates/customers/list.html` | Eliminar recurso JavaScript duplicado | Plantilla |
| `backend/app/templates/products/list.html` | Eliminar recurso JavaScript duplicado | Plantilla |
| `backend/app/customers/routes.py` | Acotar conocimiento a la página visible en vista lista | Consulta/contexto |
| `backend/app/databases/service.py` | Admitir filtros de IDs en el resumen de conocimiento | Consulta |
| `backend/app/settings/routes.py` | Reutilizar ajustes/contexto y agrupar versiones de prompts | Consultas/contexto |
| `backend/app/settings/agent_config.py` | Sustituir hidratación masiva por agregados SQL | Consulta |
| `backend/app/orders/routes.py` | Evitar segunda carga de pedidos sin filtro de estado | Flujo de consulta |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/development/performance-audit-anchi-gemavi-2026-09-02.md` | Registrar mediciones, diagnóstico, plan urgente y estado de implementación. |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Priorizar backend y consultas | Las páginas tardan 2–9 s aunque el HTML comprimido sea relativamente pequeño. | Empezar por minificar CSS sin resolver las esperas de servidor. |
| Mantener capacidades y diferir detalle | Los formularios ocultos y drawers aportan funciones, pero no deben bloquear la primera pantalla. | Eliminar editores o reducir funcionalidades. |
| Unificar el dataset de pedidos | Lista y tarjetas deben consultar una base común y evitar una segunda carga cuando comparten filtros. | Mantener dos cargas completas para cada vista. |
| Instrumentar antes de optimizar a ciegas | Producción no expone actualmente los tiempos SQL/render. | Atribuir los 9 s exclusivamente a Vercel o a la base sin medición. |

## 9. Validaciones ejecutadas

| Comando o prueba | Resultado |
|---|---|
| GET público de la aplicación | HTTP 200; primera respuesta observada ~0,9 s, `x-vercel-cache: MISS`. |
| Login demo y GET autenticados | Login correcto; no se modificaron datos. |
| 3 repeticiones de `/orders`, `/customers`, `/products`, `/settings`, `/entries` | Todas HTTP 200; `/settings` ~8,9–9,0 s, `/customers` ~5,1–5,5 s, resto según tabla. |
| Cabeceras de caché HTML | `public, must-revalidate, max-age=0`; siempre `MISS` en las mediciones. |
| Recursos CSS/JS | CSS global ~279,8 KB; `database-tables.js` duplicado bajo dos claves; recursos estáticos con caché inmutable. |
| Petición comprimida con `curl --compressed` | Brotli/gzip confirmado para recursos y páginas autenticadas. |
| `backend/.venv\Scripts\python.exe -m compileall -q app tests` | OK (`compileall=0`). |
| `backend/.venv\Scripts\python.exe -m unittest tests.test_static_assets tests.test_setup_onboarding tests.test_branding_cache tests.test_orders_list_optimization tests.test_history_filters tests.test_operational_navigation -q` | OK en ejecuciones individuales; incluye equivalencia tarjetas/lista, límites de consultas y navegación de clientes. |

## 10. Tests añadidos o modificados

No se modificaron tests. Se reutilizaron los tests de regresión y optimización existentes; `test_orders_list_optimization` valida específicamente el presupuesto de consultas y duplicados, la equivalencia de vistas y los contadores de estado.

## 11. Criterios de aceptación para la siguiente fase

| Criterio | Estado | Evidencia requerida |
|---|---|---|
| `/settings` deja de ejecutar todo el contenido pesado en la carga inicial | Parcial | Se eliminaron lecturas repetidas, N+1 de prompts y se agregaron métricas SQL; falta diferir módulos pesados. |
| Pedidos usa paginación SQL real | Parcial | Se evita la segunda carga cuando no hay estado y pasan los límites de duplicados; la paginación completa tras filtros Python sigue pendiente. |
| Bandeja filtra y pagina en servidor | Pendiente | El tiempo no crece linealmente con el histórico; solo se cargan filas visibles. |
| Clientes listados no calculan conocimiento completo | Parcial | La vista list limita el resumen a los IDs de la página actual; la vista knowledge mantiene el conjunto completo. |
| No hay recursos JS duplicados | Completado | `database-tables.js` queda una sola vez en `base.html`. |
| Primera carga no incluye formularios completos ocultos | Pendiente | Detalle cargado al abrir; HTML inicial reducido al shell y filas visibles. |
| Observabilidad disponible en producción | Pendiente | Sigue requiriendo activación/configuración de observabilidad en el despliegue. |
| No se rompen filtros, tarjetas, lista ni edición | Completado en local | Suite específica de pedidos y navegación operativa pasan en `backend/.venv`. |

## 12. Riesgos y observaciones pendientes

- La URL pública puede estar ejecutando un commit diferente al checkout local; estas mejoras aún no están desplegadas.
- Los tiempos externos dependen de la ruta de red y de la carga de Vercel/Neon; deben complementarse con métricas internas y percentiles p50/p95/p99.
- El `Cache-Control: public` en HTML autenticado debe revisarse por seguridad y coherencia de sesión. El HTML personalizado no debería quedar compartible en cachés intermedias; mantener la caché larga solo para assets inmutables.
- Las optimizaciones de paginación requieren comprobar que los contadores y filtros mantengan exactamente la semántica actual.
- No se debe cachear HTML personalizado por tenant/usuario como atajo sin definir invalidación, aislamiento y privacidad.

## 13. Desviaciones respecto al alcance inicial

El alcance inicial era únicamente analizar. El usuario pidió después aplicar lo más importante, por lo que se implementaron optimizaciones de bajo riesgo en el checkout local. La medición de navegador completo/Core Web Vitals queda pendiente por indisponibilidad de la herramienta integrada.

## 14. Estado final de Git

El repositorio contiene este informe y los cambios locales de rendimiento enumerados en la sección 6. No se han tocado secretos, datos ni configuración de producción. No se ha hecho commit ni push.

## 15. Recomendación para la siguiente fase

Ejecutar en este orden:

1. Añadir observabilidad interna temporal para separar cold start, base de datos y renderizado.
2. Atacar `/settings` con shell y cargas diferidas de módulos pesados.
3. Rehacer la carga de pedidos/bandeja para paginar y contar en SQL incluso con filtros derivados.
4. Convertir los formularios ocultos de clientes/productos en un editor bajo demanda.
5. Dividir CSS solo después de medir cobertura y verificar que no se pierde el tema global.
6. Repetir la prueba autenticada con p50/p95 y una base de datos con volumen representativo antes de publicar.
