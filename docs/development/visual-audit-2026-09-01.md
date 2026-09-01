# Auditoría visual y de experiencia de Anchi

**Fecha:** 1 de septiembre de 2026  
**Entorno:** demo local en `http://127.0.0.1:8000`  
**Resolución principal:** 1600 × 1000 CSS px  
**Alcance:** revisión visual y de interacción de las áreas accesibles desde la aplicación autenticada, con datos demo. No se ha usado producción, Meta ni cuentas reales.

## 1. Objetivo

Comprobar si la interfaz mantiene un sistema visual coherente y detectar problemas de jerarquía, densidad, navegación, estados vacíos, filtros, acciones, formularios y feedback. La revisión se ha hecho desde la perspectiva de una persona que debe gestionar pedidos, comunicaciones, clientes, productos y configuración diariamente.

## 2. Método

Se recorrieron las rutas principales y sus estados alternativos, se abrieron filtros, menús de columnas, modales, vistas divididas y opciones de configuración, y se comprobaron interacciones seguras con datos demo. Se capturaron pantallas después de cada estado relevante. No se guardaron cambios funcionales durante la auditoría.

## 3. Recorrido realizado

| Paso | Área / estado | Resultado general | Evidencia |
|---|---|---|---|
| 1 | Login | Funcional, pero con mucho espacio vacío y poca personalidad visual. | [01-login](visual-audit/2026-09-01/01-login.png) |
| 2 | Pedidos, vista de tarjetas | Funcional; la densidad y la repetición del contenido demo reducen la utilidad del resumen. | [02-dashboard-cards](visual-audit/2026-09-01/02-dashboard-cards.png) |
| 3 | Pedidos, vista de lista | Funcional; la tabla no termina de caber y la última columna queda comprometida. | [03-orders-list](visual-audit/2026-09-01/03-orders-list.png) |
| 4 | Archivos | Lista operativa, pero no muestra una acción evidente para desarchivar. | [04-orders-archive](visual-audit/2026-09-01/04-orders-archive.png) |
| 5 | Filtros avanzados de pedidos | El popover abre y contiene opciones, pero tapa parte de la tabla y no ofrece cierre/restablecimiento visible. | [05-archive-filters-open](visual-audit/2026-09-01/05-archive-filters-open.png) |
| 6 | Buzón de correo | Funcional y mejor encaminado; no todas las filas muestran los mismos controles de selección/favorito. | [06-email-inbox](visual-audit/2026-09-01/06-email-inbox.png) |
| 7 | Correo abierto | La lectura elimina la negrita correctamente; el panel de detalle concentra demasiadas acciones en una línea. | [07-email-detail](visual-audit/2026-09-01/07-email-detail.png) |
| 8 | Buzón de WhatsApp | La conversación dividida funciona y es comprensible; queda un mensaje de configuración contradictorio. | [08-whatsapp-inbox](visual-audit/2026-09-01/08-whatsapp-inbox.png) |
| 9 | Clientes, listado | Vista limpia y legible; la relación entre filas y acciones no es suficientemente visible. | [09-customers-list](visual-audit/2026-09-01/09-customers-list.png) |
| 10 | Clientes, conocimiento | La vista es útil, pero cambia mucho el patrón de listado y obliga a recorrer una página larga. | [10-customers-knowledge](visual-audit/2026-09-01/10-customers-knowledge.png) |
| 11 | Filtros de conocimiento | Funciona, aunque ofrece una versión muy distinta y más limitada del patrón de filtros. | [10a-customers-knowledge-filters-open](visual-audit/2026-09-01/10a-customers-knowledge-filters-open.png) |
| 12 | Productos | Tabla clara; las acciones por fila no resultan descubribles al pasar el cursor. | [11-products-list](visual-audit/2026-09-01/11-products-list.png), [11a-products-hover](visual-audit/2026-09-01/11a-products-hover.png) |
| 13 | Columnas de productos | El selector existe y es completo, pero el popover puede tapar contexto y no queda claro qué se ha aplicado. | [11b-products-columns-open](visual-audit/2026-09-01/11b-products-columns-open.png) |
| 14 | Configuración general | Información completa, pero hay demasiadas tarjetas y mucho espacio residual. | [13-settings-overview](visual-audit/2026-09-01/13-settings-overview.png) |
| 15 | Configuración pendiente | El porcentaje y el checklist no parecen representar exactamente los mismos requisitos. | [14-settings-pending](visual-audit/2026-09-01/14-settings-pending.png) |
| 16 | Modal del agente IA | El modal es largo y oculta contenido inferior sin una señal clara de desplazamiento. | [15-settings-ai-dialog](visual-audit/2026-09-01/15-settings-ai-dialog.png), [estado inferior](visual-audit/2026-09-01/15b-settings-ai-dialog-bottom.png) |
| 17 | Canales | Patrón de tarjetas bastante consistente; Email y WhatsApp comunican estados con distinta semántica. | [16-channel-settings](visual-audit/2026-09-01/16-channel-settings.png) |
| 18 | Configuración WhatsApp | Estado y CTA no están alineados: aparece activo, pero pendiente y con inicio de sesión deshabilitado. | [16a-whatsapp-channel-config](visual-audit/2026-09-01/16a-whatsapp-channel-config.png), [16b-webhook-details](visual-audit/2026-09-01/16b-whatsapp-webhook-details.png) |
| 19 | Configuración Email | Completa, pero excesivamente larga y con varios niveles de cajas dentro del modal. | [16c-email-channel-config](visual-audit/2026-09-01/16c-email-channel-config.png), [16d-email-advanced](visual-audit/2026-09-01/16d-email-advanced-options.png) |
| 20 | Importación manual | Flujo entendible y bien separado en entrada/propuesta, aunque con bastante contenedor anidado. | [17-manual-import](visual-audit/2026-09-01/17-manual-import.png) |
| 21 | Alertas | Información completa; las tarjetas repiten un bloque de acciones muy alto y consumen mucho espacio. | [18-alerts](visual-audit/2026-09-01/18-alerts.png) |
| 22 | Política de privacidad | Página legible y coherente como documento legal; utiliza una carcasa distinta a la aplicación. | [19-privacy](visual-audit/2026-09-01/19-privacy.png) |
| 23 | Usuarios | Formulario y tabla visibles, pero ambos compiten en el mismo plano y la edición queda poco jerarquizada. | [20-users](visual-audit/2026-09-01/20-users.png) |
| 24 | Detalle de pedido | Permite resolver el pedido; en pedidos manuales el panel de origen queda casi vacío. | [21-order-detail](visual-audit/2026-09-01/21-order-detail.png) |
| 25 | Carpeta de conocimiento de cliente | Potente, pero el panel de alertas lateral reduce el área útil y corta textos. | [22-customer-knowledge-detail](visual-audit/2026-09-01/22-customer-knowledge-detail.png) |
| 26 | Acceso directo a alta de cliente/producto | Las rutas GET probadas responden `405 Method Not Allowed` y muestran JSON sin estilo. | [23-customer-new](visual-audit/2026-09-01/23-customer-new.png), [24-product-new](visual-audit/2026-09-01/24-product-new.png) |

## 4. Hallazgos priorizados

### P1 — Corregir antes de una demo operativa seria

#### VA-01 — Requisitos pendientes desincronizados

**Evidencia:** [14-settings-pending](visual-audit/2026-09-01/14-settings-pending.png).  
La cabecera indica `2 pendientes` y `1 de 7 requisitos requieren atención`, pero el checklist visible muestra un único requisito pendiente y no representa de forma evidente el segundo pendiente de la pantalla general, FTP/SFTP. Esto hace que la persona usuaria no sepa qué falta realmente.

**Corrección recomendada:** usar una única fuente de verdad para porcentaje, contador y checklist; mostrar siempre todos los requisitos que computan en el porcentaje, con el mismo nombre y estado.

#### VA-02 — Rutas de alta responden con error visible

**Evidencia:** [23-customer-new](visual-audit/2026-09-01/23-customer-new.png), [24-product-new](visual-audit/2026-09-01/24-product-new.png).  
El acceso directo GET a ambas rutas devuelve `405 Method Not Allowed` en formato JSON. Aunque el alta pueda estar planteada mediante un formulario o diálogo POST, una ruta navegable no debería terminar en una pantalla técnica.

**Corrección recomendada:** exponer una pantalla GET de alta, o cambiar el enlace para abrir el formulario correcto. Si la operación no admite GET, devolver una redirección o una página de error integrada en la carcasa de Anchi.

#### VA-03 — Estado de WhatsApp ambiguo

**Evidencia:** [16-channel-settings](visual-audit/2026-09-01/16-channel-settings.png), [16a-whatsapp-channel-config](visual-audit/2026-09-01/16a-whatsapp-channel-config.png).  
El canal se presenta como `Activo · pendiente de conectar`, mientras que la configuración informa de variables faltantes y deja deshabilitado `Iniciar sesión con Meta`. El usuario puede interpretar que el canal ya recibe mensajes cuando todavía no está operativo.

**Corrección recomendada:** separar `habilitado` de `conectado` y usar estados inequívocos: `Deshabilitado`, `Configuración incompleta`, `Listo para conectar`, `Conectado` o `Error`. El CTA principal debe explicar el siguiente paso y su causa si está deshabilitado.

#### VA-04 — Mensaje de WhatsApp contradictorio con el estado de la conversación

**Evidencia:** [08-whatsapp-inbox](visual-audit/2026-09-01/08-whatsapp-inbox.png).  
La conversación y el compositor de respuesta están disponibles, pero permanece un texto que dice que hay que conectar/activar WhatsApp para responder. Es una instrucción stale que mina la confianza en el flujo.

**Corrección recomendada:** renderizar el mensaje solo cuando el canal realmente esté inactivo o no permita responder; en estado operativo mostrar estado de conexión y límites de envío.

#### VA-05 — Acción de desarchivar no descubrible

**Evidencia:** [04-orders-archive](visual-audit/2026-09-01/04-orders-archive.png).  
La vista Archivos muestra el pedido archivado, pero no ofrece una acción visible para desarchivarlo ni aparece una acción clara al pasar el cursor.

**Corrección recomendada:** incluir `Desarchivar` en las acciones de fila y en el menú contextual, con confirmación no destructiva y feedback posterior.

#### VA-06 — Tabla de pedidos recortada en el ancho normal

**Evidencia:** [03-orders-list](visual-audit/2026-09-01/03-orders-list.png).  
La última columna queda comprometida en 1600 px y obliga a descubrir el contenido mediante desplazamiento horizontal o deja una acción parcialmente fuera de contexto.

**Corrección recomendada:** reservar una columna de acciones fija, permitir ocultar columnas secundarias, establecer anchos mínimos razonables y mantener visible el identificador/estado prioritario.

### P2 — Mejorar en la siguiente iteración visual

#### VA-07 — Sistema de filtros fragmentado

Pedidos, correo, clientes, conocimiento, productos y alertas usan combinaciones distintas de chips, selects, botones, popovers y textos. El patrón de pedidos es el más completo, pero no se reutiliza de forma consistente.

**Corrección recomendada:** crear un componente de barra de filtros con: filtros rápidos, `Más filtros` con icono, contador de filtros activos, chips eliminables, `Aplicar` y `Limpiar`. Permitir variantes por dominio sin cambiar la gramática visual.

#### VA-08 — Popovers difíciles de contextualizar

El popover de filtros avanzados aparece alineado a la derecha y tapa parte del contenido; no tiene cierre visible y el selector de columnas no deja suficientemente claro si los cambios se aplican de inmediato.

**Corrección recomendada:** anclar el popover al botón, añadir cierre con `Esc`/botón, mantener el foco accesible y mostrar el número de filtros/columnas activos.

#### VA-09 — Controles de correo inconsistentes por fila

**Evidencia:** [06-email-inbox](visual-audit/2026-09-01/06-email-inbox.png).  
Varias filas tienen checkbox y estrella, pero otras filas no muestran esos controles. Esto rompe la expectativa de acciones en masa y hace difícil distinguir si una fila está seleccionable o si simplemente falta el control.

**Corrección recomendada:** reservar siempre las mismas columnas para selección y favorito; deshabilitar explícitamente cuando no proceda y explicar el motivo con tooltip.

#### VA-10 — Modales de configuración demasiado largos

**Evidencia:** [15-settings-ai-dialog](visual-audit/2026-09-01/15-settings-ai-dialog.png), [16d-email-advanced](visual-audit/2026-09-01/16d-email-advanced-options.png).  
El modal del agente IA y el de Email reúnen demasiadas secciones; el contenido inferior queda fuera del primer viewport y no hay una señal clara de scroll. La configuración SMTP/IMAP termina pareciendo una página dentro de otra página.

**Corrección recomendada:** convertir los modales largos en páginas de configuración o paneles por pestañas/secciones; mantener una barra de acciones fija y un resumen de estado persistente.

#### VA-11 — Exceso de cajas anidadas

En configuración, importación manual, detalle de cliente y alertas se repite la estructura “contenedor > tarjeta > tarjeta > campos”. El resultado es visualmente pesado y dificulta distinguir agrupación de interacción.

**Corrección recomendada:** limitar los bordes a una jerarquía principal, usar separación, encabezados y fondos suaves para subgrupos, y reservar las tarjetas para unidades realmente independientes.

#### VA-12 — Vistas de clientes no comparten suficiente lenguaje visual

**Evidencia:** [09-customers-list](visual-audit/2026-09-01/09-customers-list.png), [10-customers-knowledge](visual-audit/2026-09-01/10-customers-knowledge.png).  
Listado y conocimiento tienen funciones relacionadas, pero cambian de tabla a tarjetas con ritmos, acciones y densidad diferentes. La vista de conocimiento además se alarga mucho con 20 tarjetas.

**Corrección recomendada:** mantener la misma cabecera, toolbar, paginación, menú de acciones y estados; cambiar solo el contenido principal. Añadir paginación o una densidad compacta para carpetas.

#### VA-13 — Acciones de productos poco visibles

**Evidencia:** [11-products-list](visual-audit/2026-09-01/11-products-list.png), [11a-products-hover](visual-audit/2026-09-01/11a-products-hover.png).  
La tabla no comunica claramente cómo editar una fila. En clientes y pedidos existen botones/menús más evidentes, pero productos no mantiene el mismo affordance.

**Corrección recomendada:** añadir menú de fila consistente (`Editar`, `Abrir`, `Eliminar`) visible al hover y accesible con teclado; conservar una acción primaria clara.

#### VA-14 — Panel de alertas lateral invade el detalle de cliente

**Evidencia:** [22-customer-knowledge-detail](visual-audit/2026-09-01/22-customer-knowledge-detail.png).  
El panel lateral ocupa un área amplia y corta títulos/descripciones de las alertas. El detalle de conocimiento queda comprimido aunque la página tenga espacio vertical.

**Corrección recomendada:** usar panel colapsable, ancho mínimo con truncado intencional y tooltip, o mover alertas a una bandeja contextual accesible desde el header.

#### VA-15 — Consistencia tipográfica y microcopy

Se observan variantes como `Pagina 1 de 1`/`Última`, etiquetas sin acento y textos de ayuda largos junto a controles que ya son autoexplicativos. También conviven títulos y subtítulos repetidos en varias páginas.

**Corrección recomendada:** centralizar traducciones/microcopy, revisar acentos y establecer una jerarquía fija: título de página, contexto opcional y toolbar. Eliminar el texto descriptivo cuando no añade una decisión útil.

### P3 — Pulido posterior

#### VA-16 — Aprovechamiento del espacio en páginas cortas

Login, pedidos archivados, usuarios y varias vistas dejan grandes áreas vacías. No es necesariamente un error, pero sí hace que la aplicación parezca inacabada y aumenta la distancia entre acciones relacionadas.

**Corrección recomendada:** usar layouts con ancho máximo coherente, paneles de resumen o estados vacíos útiles, sin rellenar por rellenar.

#### VA-17 — Detalle de pedidos manuales con origen vacío

**Evidencia:** [21-order-detail](visual-audit/2026-09-01/21-order-detail.png).  
El panel izquierdo ocupa aproximadamente la mitad de la pantalla para mostrar que no existe mensaje original. En un pedido creado manualmente, ese estado podría comunicarse en un bloque compacto para dar más espacio a la resolución.

#### VA-18 — Formulario de usuarios poco jerarquizado

**Evidencia:** [20-users](visual-audit/2026-09-01/20-users.png).  
Crear usuario y editar usuario aparecen como una tarjeta de formulario y una tabla de una sola fila sin separación conceptual fuerte. El campo de contraseña “Mantener” puede confundirse con una contraseña actual.

**Corrección recomendada:** separar “Invitar/crear” de “Usuarios existentes”, renombrar el placeholder y usar una acción de restablecimiento explícita.

#### VA-19 — Política legal con carcasa independiente

**Evidencia:** [19-privacy](visual-audit/2026-09-01/19-privacy.png).  
La página es legible y apropiada como documento público, pero no comparte navegación ni espaciado con login. Conviene decidir deliberadamente si las páginas legales son una experiencia externa o parte de Anchi.

## 5. Hallazgos técnicos detectados durante la inspección

- La consola del navegador emitió 10 warnings en `/products`: varios `input[type=date]` reciben el valor literal `None`, que no cumple el formato `yyyy-MM-dd`. No rompió la pantalla, pero puede impedir filtros/edición de fechas y debe corregirse normalizando `None` a vacío antes de renderizar.
- Las rutas GET de alta de cliente y producto respondieron 405. Es un error de navegación/contrato de ruta, aunque no se ha asumido cuál es el flujo de alta previsto.
- No se detectaron errores de consola en las pantallas principales aparte de los warnings de productos y el error esperado de las rutas 405.

## 6. Lo que está funcionando bien

- La carcasa principal, navegación lateral y header superior forman una base reconocible.
- Pedidos y correo ya tienen patrones útiles de filtros, estados, paginación y feedback.
- La vista partida de WhatsApp comunica bien la relación contactos/conversación y el compositor soporta documentos y audio de forma visible.
- La tabla de clientes y la de productos tienen buena legibilidad de datos cuando el ancho es suficiente.
- La página de privacidad tiene una lectura clara, una jerarquía correcta y una presentación adecuada para revisión externa.
- Abrir un correo actualiza el estado visual y elimina la negrita del título, tal como se esperaba.

## 7. Plan recomendado de corrección

1. Unificar el modelo de estados y requisitos de configuración; corregir WhatsApp y el checklist pendiente.
2. Resolver rutas de alta y acción de desarchivar.
3. Corregir el layout de tablas, columna de acciones y controles de selección/favorito del correo.
4. Crear un patrón común de filtros/popovers/columnas.
5. Reducir cajas anidadas y convertir modales largos en páginas o paneles con navegación interna.
6. Aplicar el mismo sistema a Clientes, Productos, Alertas y Configuración.
7. Hacer una pasada de microcopy, fechas, acentos, estados vacíos y responsive.

## 8. Limitaciones

- La auditoría se realizó en escritorio a 1600 × 1000; no incluye todavía una ronda móvil/tablet.
- Se utilizaron datos demo locales; no se validaron Meta, SMTP, Vercel ni ningún flujo productivo.
- No se ha hecho una auditoría exhaustiva de accesibilidad con lector de pantalla ni contraste automatizado.
- No se han modificado archivos de código, datos, configuración ni dependencias durante esta revisión.

## 9. Validaciones ejecutadas

| Comando / comprobación | Resultado |
|---|---|
| `py -3 -m compileall -q backend/app` | Correcto. |
| `git diff --check` | Correcto; no hay diff de código ni errores de whitespace en cambios tracked. |
| Recorrido Playwright de 26 rutas/estados locales | Completado; se generaron 31 capturas, incluyendo estados alternativos. |
| Consola del navegador | Sin errores en las pantallas principales; 10 warnings en Productos por fechas con valor literal `None`. |
| `py -3 -m unittest discover -s backend/tests -q` | No concluyó tras más de 90 segundos; se detuvieron únicamente los dos procesos de test lanzados para evitar dejar procesos colgados. No se obtuvo un resultado fiable de la suite completa. |

## 10. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/development/visual-audit-2026-09-01.md` | Informe de auditoría, evidencias y plan priorizado. |
| `docs/development/visual-audit/2026-09-01/*.png` | Capturas locales de las pantallas y estados auditados. |

No hay archivos funcionales modificados.

## 9. Entrega y estado de Git

Este documento y las capturas son los únicos artefactos de esta auditoría. No hay cambios de código funcional. Se publica únicamente `docs/development/visual-audit-2026-09-01.md` y `docs/development/visual-audit/2026-09-01/`; el directorio temporal `.playwright-cli/` queda excluido.
