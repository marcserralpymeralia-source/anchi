# Matriz de variantes Sage para ANCHI

Fecha de corte: 2026-09-30
Alcance: discovery documental de conectores; no se ha implementado runtime, no se han instalado dependencias y no se han ejecutado llamadas contra Sage.

## Regla de identificación

“Sage” no es una API única. Las siguientes familias deben tratarse como productos y contratos separados:

- Sage 200 España.
- Sage 200 UK/Irlanda.
- Sage Active, con entorno ES/PT.
- Sage 50 España.
- Sage X3.
- Sage 300.

No se debe registrar un conector genérico `sage` que esconda diferencias de producto, país, edición, versión, tenancy, autenticación y modelo de documentos.

## Variantes estudiadas

| Variante | Mercado relevante | Superficie pública observada | Clientes/productos | Pedidos | Precios/stock | Autenticación y despliegue | Encaje inicial |
|---|---|---|---|---|---|---|---|
| **Sage 200 España — Soluciones Conectadas / Plataforma Connect** | España; objetivo prioritario para una primera prueba de mercado | Sage documenta que Sage 200 puede compartir datos de ventas con otras aplicaciones mediante API. El acceso exige licencia de Soluciones Conectadas, Sage 200 Advanced y versión mínima 2020.87.000. El portal de ayuda no expone en abierto un catálogo completo de rutas o esquemas. | **Parcialmente conocido**: la administración permite seleccionar entidad, tabla y campo mediante propiedades extendidas; el mapeo exacto de clientes/productos queda por verificar en portal de desarrolladores o tenant de prueba. | La capacidad de compartir ventas está documentada; creación, lectura de pedido, estados y correlación externa deben probarse. | **Desconocido para el contrato público consultado**; no asumir tarifa, stock ni disponibilidad. | Activación por administrador, alias, Sage ID, invitaciones, usuario de aplicación y permisos por empresa. Sage 200 España puede ser local/desktop, online o alojado en Azure; la topología concreta del tenant debe confirmarse. | **Candidato recomendado para Block4B**, condicionado a confirmar clientes, productos, precio, stock, pedido y reconciliación.
| **Sage Active — API pública V2, entorno ES/PT** | España/Portugal; producto cloud moderno | GraphQL público documentado; recursos para customers, products, sales tariffs/discounts, sales quotes/orders/delivery notes/invoices y reglas de legislación ES. Endpoint ES/PT y colección Postman están documentados. | **Sí, documentado**: customers y products; organización, legislación y campos dependen del entorno. | **Sí, documentado**: `createSalesOrder`, lectura/listado y líneas; devuelve `id` y `operationalNumber`. | **Sí, documentado**: tarifas, líneas de tarifa, escalados, descuentos y servicio de cálculo de precio; stock debe confirmarse para el flujo concreto. | OAuth 2.0; PKCE para SPA, bearer token, scopes, app registration, subscription key y `X-OrganizationId`/`LegislationCode` en los ejemplos. Cloud-only. | **Alternativa técnicamente más demostrable**. Debe ganar prioridad si los clientes de GEMAVI son mayoritariamente Sage Active.
| **Sage 200 UK/Irlanda** | Reino Unido/Irlanda; no es la variante española | Portal de desarrolladores REST con diferencias entre Standard Online y Professional; headers `X-Site`/`X-Company` y OAuth 2.0 documentados. | Amplia superficie documentada para el mercado UKI. | API de ventas documentada para UKI. | Depende de edición, compañía y endpoints UKI. | OAuth 2.0, developer account y límites/políticas del portal UKI. | **No seleccionar para ANCHI España** salvo cliente explícito UKI. No extrapolar sus rutas a Sage 200 España.
| **Sage 50 España** | España; potencialmente frecuente en pymes | Sage ofrece “Soluciones Conectadas” y conectores/partners, pero la investigación no encontró un contrato público general equivalente a Sage Active. Una declaración oficial menciona conectividad con Office 365, fintech, soluciones de terceros y desarrollos a medida, no un API REST público completo. | **No verificable con fuentes públicas consultadas**. | **No verificable**. | **No verificable**. | Puede ser local/desktop o cloud según producto; probable dependencia de partner, SDK o conector comercial. | **No recomendar como primer API connector** hasta obtener documentación/licencia y una instalación de prueba.
| **Sage X3** | Enterprise, fabricación/distribución; no el perfil de primera prueba de ANCHI | REST/SOAP oficiales; GraphQL para operaciones en tiempo real y ficheros para bulk según la documentación del portal. | Amplia superficie empresarial, pero dependiente de endpoint/folder y configuración. | Sí, con REST/SOAP/GraphQL según operación. | Sí, potencialmente, pero el contrato real es instance-specific. | Cloud/on-prem; aplicaciones conectadas, JWT/OAuth y configuración de endpoint/folder. | **Referencia futura**, no primer conector del Order Hub.
| **Sage 300** | Norteamérica/Canadá y otros mercados; fuera del foco español | Web API propia; el cliente Ruby `sagekit` muestra rutas OData y compañía en URL. | Sí, pero producto y operación distintos. | Sí, según módulos. | Dependiente de módulos y configuración. | Normalmente servicio web/IIS y credenciales del entorno. | **Fuera de alcance**; no confundir con Sage 200.

## Lectura de la matriz

1. El nombre de clase recomendado para Block4B es **`Sage200ESConnector`**, no `SageConnector`.
2. El alcance de esa clase debe declarar explícitamente `product = sage_200`, `country = ES`, `surface = soluciones_conectadas` y una versión/feature set descubierto en el tenant.
3. `SageActiveESConnector` debe permanecer como alternativa de diseño, no mezclarse en el mismo adapter ni inferirse desde la documentación de Sage 200.
4. Sage 200 UKI sirve para entender el patrón de una API Sage 200, pero sus rutas, headers y autorización no son evidencia válida para España.
5. Para Sage 50 España, la ausencia de un contrato REST público verificable es una señal de discovery pendiente, no prueba de que el producto carezca de integraciones.

## Criterio de selección

La selección combina mercado objetivo, contrato oficial, facilidad de onboarding y cobertura mínima del contrato ANCHI:

- **Mercado**: Sage 200 España es la opción con señal directa de producto y API para el país objetivo.
- **Contrato**: Sage Active tiene mejor documentación pública de clientes, catálogo, precios y pedidos.
- **Onboarding**: Sage 200 España exige coordinación con administrador, licencia, edición, versión y permisos; esto eleva el coste, pero hace explícitos los prerequisitos.
- **Riesgo**: Sage 200 España conserva incertidumbre sobre endpoints y correlación; Sage Active conserva incertidumbre sobre representatividad de la base instalada de GEMAVI.

Por tanto, la decisión para este bloque es: **preparar el POC de `Sage200ESConnector` y bloquear la implementación hasta que un cliente piloto confirme la variante; si ese piloto es Sage Active, cambiar la selección a `SageActiveESConnector` antes de escribir código**.

## Fuentes principales

- [Sage 200 España — Soluciones Conectadas](https://sage200c.sage.es/help/Content/Conect/Sage-soluciones-conectadas.htm)
- [Sage 200 España — esquema funcional / Plataforma Connect](https://sage200c.sage.es/help/Content/Introduccion/esquema-funcional.htm)
- [Sage 200 UK developer portal](https://developer.sage.com/200-uk)
- [Sage Active — quick start y API V2](https://developer.sage.com/active/docs/latest/g/quickstart?tab=FR)
- [Sage Active — recursos](https://developer.sage.com/active/docs/latest/g/resources)
- [Sage Active — Sales Orders](https://developer.sage.com/sageactive/resources/salesorders/)
- [Sage Active — product pricing/tariffs](https://developer.sage.com/active/docs/latest/g/resources/specific/product)
- [Sage 50 España — Soluciones Conectadas](https://www.sage.com/es-es/productos/sage-50cloud/soluciones-conectadas/)
- [Sage 200 España — declaración oficial de producto](https://www.sage.com/es-es/-/media/files/sagedotcom/spain/documents/pdf/certificados/declaracion_responsable_sage200_30-4-25.pdf)
- [Sage X3 — API guide](https://developer.sage.com/x3/docs/latest/guides/x3-development-platform/platform-reference/api-guide)
- [Sage X3 — integration overview](https://developer.sage.com/x3/docs/latest/guides/overview?tab=A)
- [Sage 300 Web API client de referencia](https://github.com/lepepe/sagekit)
