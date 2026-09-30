# Análisis de API Sage para ANCHI

Fecha de corte: 2026-09-30
Conclusión: el primer POC debe investigar **Sage 200 España / Soluciones Conectadas** como conector de mercado, manteniendo Sage Active ES/PT como alternativa técnica con mayor evidencia pública de API.

## A. Variantes y producto/versionado

### Sage 200 España

El producto de referencia es **Sage 200 Advanced con licencia Sage Soluciones Conectadas**, también descrito por Sage España como Plataforma Connect. La ayuda oficial indica que la API se habilita con un alias, Sage ID, invitaciones y permisos por usuario/empresa. También fija una versión mínima documentada de **2020.87.000** para el acceso a Soluciones Conectadas.

Esto no autoriza a asumir que cualquier Sage 200 español tiene el mismo contrato: hay que capturar en onboarding edición, versión, modalidad de alojamiento, empresas habilitadas y feature set disponible.

### Sage Active

Sage Active expone una API pública V2 GraphQL con entornos FR, ES/PT y DE. La documentación pública es más detallada que la de Sage 200 España: describe organizaciones, legislación, clientes, productos, tarifas, descuentos y documentos de venta. El entorno ES/PT usa una API GraphQL y credenciales específicas del entorno.

### Variantes descartadas para el primer POC

- **Sage 200 UKI**: sus endpoints, headers y autorización pertenecen a Reino Unido/Irlanda.
- **Sage 50 España**: se observan conectores y soluciones asociadas, pero no se ha verificado un API público completo para clientes/productos/pedidos.
- **Sage X3**: tiene una API potente, pero su complejidad, configuración por folder/endpoint y perfil enterprise no corresponden a la primera prueba.
- **Sage 300**: API y mercado distintos; no debe usarse como proxy de Sage 200.

## B. ¿Qué API o APIs concretas existen en España?

### Respuesta verificada

Para Sage 200 España, la fuente oficial confirma una **API de Sage Soluciones Conectadas** y remite al portal de desarrolladores para soluciones a medida. La documentación consultada confirma:

- compartir datos de ventas con otras aplicaciones;
- licencia de Soluciones Conectadas;
- edición Sage 200 Advanced;
- versión mínima 2020.87.000;
- activación administrativa;
- alias y Sage ID;
- invitación de usuarios;
- selección del usuario de aplicación de Sage 200;
- restricción a las empresas que ese usuario puede consultar;
- propiedades extendidas para añadir entidad, tabla y campo.

### Lo que no está verificado públicamente

No se ha encontrado en la documentación pública consultada un contrato completo y versionado de:

- base URL y rutas exactas;
- esquema de cliente, producto, precio, stock y pedido;
- operaciones de creación/lectura de pedido;
- filtro/paginación y límites;
- claves de idempotencia;
- headers de correlación;
- códigos de error y clasificación retryable;
- sandbox o tenant demo público;
- política de rate limit específica para España.

Estos puntos son **UNKNOWN deliberados** y constituyen gates de Block4B. No deben rellenarse con la API de Sage 200 UKI ni con inferencias de Sage Active.

### APIs españolas documentadas con mayor precisión

Para Sage Active ES/PT sí hay documentación pública concreta:

- GraphQL: `https://api.es.active.sage.com/graphql` en la colección Postman oficial.
- OAuth authorize/token y scopes documentados en esa colección.
- `X-OrganizationId` y `LegislationCode` para seleccionar organización y legislación.
- Consultas/mutaciones de clientes, productos, ventas y listas.
- `createSalesOrder`, lectura/listado de sales orders y líneas.

La existencia de esta API no implica que sea el primer conector comercial de GEMAVI; la representatividad de clientes debe confirmarse.

## C. Diferencias por producto y versión

| Dimensión | Sage 200 ES | Sage Active ES/PT | Sage 200 UKI | Consecuencia en ANCHI |
|---|---|---|---|---|
| Producto | ERP Sage 200 Advanced / Soluciones Conectadas | ERP cloud Sage Active | Sage 200 Standard/Professional UKI | Identidad del connector obligatoria |
| País/legislación | España; empresas y permisos locales | Entorno ES/PT y `LegislationCode` | UK/Irlanda | No reutilizar mappings fiscales sin verificar |
| API | Documentada a alto nivel; contrato completo gated | GraphQL público | REST documentado | Interfaces separadas |
| Selección de tenant/empresa | Usuario Sage 200 y empresas accesibles | `X-OrganizationId`/organización | `X-Company` y `X-Site` | Tenant context explícito y secreto fuera del payload |
| Autenticación | Sage ID, alias, invitación, usuario de aplicación; detalles de token a confirmar | OAuth 2.0, app registration, scopes y bearer | OAuth 2.0 UKI | Estrategias de auth distintas |
| Clientes/productos | Confirmar entidades y campos con portal/tenant | Documentados | Documentados para UKI | Capabilities negociadas, no hardcodeadas |
| Precio | No verificado en contrato público ES | Tarifas/descuentos/product price service documentados | Depende de API UKI | Price mode debe ser capability |
| Pedido | Compartir ventas confirmado; create/read/status por probar | Create/read/list confirmado | API UKI | POC debe demostrar pedido mínimo |
| Idempotencia | No pública/confirmada | No debe asumirse aunque exista mutation id | No extrapolar | Idempotencia local + reconciliación |
| Despliegue | Local/online/Azure según producto y tenant | Cloud | Online/Professional según contrato | Evaluar egress, allowlist y conectividad |

## D. Mapeo contra el contrato ANCHI

El contrato actual de ANCHI declara capacidades como `customers.read`, `products.read`, `pricing.read`, `inventory.read`, `orders.create`, `orders.read`, `catalog.read` y `catalog.write`. La recomendación de POC no declara todas esas capacidades por anticipado.

### Capability matrix para Sage 200 ES

| Capacidad ANCHI | Estado | Evidencia/gate |
|---|---|---|
| `customers.read` | **PROVISIONAL** | Debe identificarse entidad, clave estable, filtros y permisos por empresa |
| `products.read` | **PROVISIONAL** | Debe identificarse tabla/código, descripción, unidad y estado |
| `pricing.read` | **UNKNOWN** | Confirmar tarifas, cliente/tarifa, impuestos, moneda y fecha de vigencia |
| `inventory.read` | **UNKNOWN** | Confirmar existencia, almacén, reserva y latencia |
| `orders.create` | **UNKNOWN** | Confirmar operación de escritura y validaciones de negocio |
| `orders.read` | **PROVISIONAL** | Confirmar consulta por ID, estado y fecha |
| `catalog.read` | **PROVISIONAL** | Puede componerse de productos; no equivale automáticamente a catálogo comercial |
| `catalog.write` | **NOT IN POC** | Fuera de alcance hasta demostrar permisos y semantics de publicación |

### Capability matrix para Sage Active ES/PT

| Capacidad ANCHI | Estado |
|---|---|
| `customers.read` | Documentada públicamente |
| `products.read` | Documentada públicamente |
| `pricing.read` | Documentada para tarifas/descuentos y cálculo de precio; confirmar payload final |
| `inventory.read` | Requiere confirmar recurso/permiso y formato para el caso GEMAVI |
| `orders.create` | Documentada mediante `createSalesOrder` |
| `orders.read` | Documentada mediante query/listado de sales orders |
| `catalog.read` | Componible desde products/lists; confirmar la representación deseada |
| `catalog.write` | No forma parte del primer POC |

## E. Precio, licencias y fuente autoritativa

No se ha encontrado una tabla pública de precio del API de Sage 200 España ni un precio universal del connector. Lo que sí puede afirmarse:

- el acceso de Sage 200 España depende de la licencia comercial de **Sage Soluciones Conectadas**, Sage 200 Advanced y la versión mínima indicada por Sage;
- Sage Active es un producto comercial con app registration/API credentials; la documentación técnica no sustituye la confirmación contractual de plan, cuotas o límites;
- Sage 50 España muestra soluciones conectadas y partners, pero no se puede convertir esa página en evidencia de un API REST general;
- las condiciones, límites y precio deben venir de Sage/partner autorizado para el producto, país y edición del cliente.

Fuente autoritativa recomendada para cerrar precio y autorización:

1. contrato/licencia del cliente;
2. portal oficial de desarrolladores del producto;
3. administrador Sage del cliente;
4. partner Sage autorizado, con confirmación escrita del alcance API;
5. tenant de prueba no productivo.

No usar repositorios comunitarios ni páginas de agregadores como fuente de precio, compatibilidad o permiso de redistribución.

## F. Identidad de producto y cliente

### Identidad Sage 200 ES

La configuración debe guardar, como mínimo, metadatos no secretos:

- `provider = sage`;
- `product = sage_200`;
- `country = ES`;
- `surface = connected_solutions`;
- `edition = advanced`;
- `sage_version` y fecha de comprobación;
- `company_scope` o lista de empresas autorizadas, sin asumir que coincide con `company_id` de ANCHI;
- `connector_capabilities` negociadas y fuente de la evidencia.

### Identidad de clientes y productos

No usar nombre o SKU visible como clave única. El mapping debe tener:

- `external_system = sage_200_es`;
- tipo de entidad (`customer`, `product`, `order`, etc.);
- ID/código nativo Sage;
- `company_id` ANCHI;
- versión o snapshot de mapping;
- fecha de última confirmación;
- reglas de resolución y estado (`confirmed`, `ambiguous`, `missing`, `disabled`).

El POC debe demostrar si la clave estable es un ID interno, código de cliente/producto o combinación empresa+clave. No se debe imponer un UUID de ANCHI en Sage sin confirmar que existe un campo de referencia externa soportado.

## G. Idempotencia y reconciliación

### Conclusión

No hay evidencia pública suficiente para atribuir a Sage 200 España una idempotency key nativa. Por diseño, ANCHI debe protegerse aunque el proveedor sea idempotente internamente.

### Patrón obligatorio de ANCHI

Para cada exportación de pedido:

1. generar una `idempotency_key` determinista por tenant, pedido y versión de efecto;
2. persistir payload hash, estado y timestamps en outbox/delivery attempt;
3. enviar una referencia externa estable si Sage ofrece un campo aceptado;
4. si el timeout deja el commit remoto desconocido, no repetir ciegamente;
5. consultar por referencia externa, número de pedido, hash o ventana temporal según el contrato comprobado;
6. registrar `remote_order_id`, estado y evidencia de reconciliación;
7. marcar `remote_commit_unknown` cuando no exista consulta segura.

### Preguntas Block4B

- ¿Existe header/campo de idempotencia?
- ¿Qué ocurre al repetir exactamente el mismo payload?
- ¿Se puede buscar un pedido por referencia externa antes de crear?
- ¿Qué identificador devuelve la creación?
- ¿Los estados son monotónicos y consultables?
- ¿Qué errores indican validación de negocio frente a timeout/transporte?
- ¿Hay eventos, webhook o solo polling?

## H. Autenticación y despliegue

### Sage 200 ES

El onboarding debe empezar por el administrador Sage:

1. validar licencia de Soluciones Conectadas;
2. validar edición Advanced y versión mínima;
3. activar el acceso y crear alias/Sage ID;
4. invitar a la identidad técnica o de integración;
5. asociar usuario de aplicación Sage 200;
6. limitar empresas accesibles al mínimo;
7. confirmar el método de token, scopes/roles y expiración en documentación/tenant;
8. entregar a ANCHI únicamente secretos por canal seguro.

No se usarán usuarios personales, contraseñas de producción en fixtures ni acceso directo a SQL. Si el cliente es local/on-premise, el POC debe resolver si la API es accesible por HTTPS desde ANCHI, mediante allowlist, VPN o agente saliente; no se debe abrir inbound sin una decisión de infraestructura explícita.

### Sage Active

El flujo documentado usa app registration, OAuth 2.0, scopes y bearer token; la colección Postman oficial muestra endpoint ES/PT, token endpoint, subscription key, `Client ID/Secret`, `X-OrganizationId` y `LegislationCode`. Estos valores deben vivir en secret manager/configuración por tenant, nunca en el dominio canónico ni en logs.

## I. Descubrimiento GitHub/open source

| Proyecto | Producto | Licencia/estado observado | Clasificación | Decisión |
|---|---|---|---|---|
| [Sage-ERP-X3/sample-x3-ws-php-webportal](https://github.com/Sage-ERP-X3/sample-x3-ws-php-webportal) | X3 | Repositorio oficial de ejemplos; declara que es presales/developer y no producción; no se ha usado como dependencia | REFERENCE | Útil para auth/payloads X3, irrelevante para Sage 200 ES |
| [lepepe/sagekit](https://github.com/lepepe/sagekit) | Sage 300 | MIT; Ruby; API Web de Sage 300 | REJECT | Producto y mercado distintos; no copiar ni adaptar al primer connector |
| [Dankular/Sage50Accounts](https://github.com/Dankular/Sage50Accounts) | Sage 50 Accounts UK | MIT; Windows/.NET; SDO/COM local; UK | REJECT | No es Sage 50 España ni API cloud; riesgo operativo y de licencia de SDK |
| [api-evangelist/sage-x3](https://github.com/api-evangelist/sage-x3) | X3 | Perfil independiente, no fuente contractual de Sage | REFERENCE | Sirve para descubrir URLs/documentos; verificar todo contra Sage |

No se ha encontrado un SDK público mantenido y específico para **Sage 200 España** o **Sage Active ES/PT** que sea claramente mejor que un cliente HTTP/GraphQL propio. La ausencia de candidato no justifica instalar una librería de otro producto.

## J. Primer connector recomendado

**Recomendación operativa: `Sage200ESConnector`, limitado a Sage 200 Advanced + Soluciones Conectadas, y solo después de que un cliente piloto confirme el tenant y el contrato.**

Motivos:

- es la variante con evidencia directa de API dentro del país objetivo;
- sus prerrequisitos de licencia, versión y permisos son explícitos y pueden convertirse en checklist de onboarding;
- conecta con el perfil ERP de ANCHI sin forzar el modelo de Sage Active;
- permite validar el principal riesgo real: si el contrato español soporta de forma segura clientes, catálogo/precio, stock y creación/reconciliación de pedidos.

Condición de cambio: si el primer cliente de GEMAVI usa Sage Active, el connector seleccionado debe ser `SageActiveESConnector`; no se debe desarrollar Sage 200 ES “por analogía”.

No se recomienda `SageConnector` genérico, ni un connector basado en Sage 200 UKI, Sage 300, Sage 50 UK SDK o Sage X3.

## K. Checklist breve de cliente

El checklist operativo está en [sage-client-discovery-checklist.md](./sage-client-discovery-checklist.md). Ningún campo de secretos debe entrar en un issue, documento compartido, fixture o captura de pantalla.

## L. Plan de Block4B

El plan ejecutable, con gates y criterios de salida, está en [block4b-sage-poc-plan.md](../development/block4b-sage-poc-plan.md). Block4B sigue siendo un POC controlado: fixture/tenant no productivo, sin datos de producción, sin credenciales versionadas y sin activar aún un connector en runtime.
