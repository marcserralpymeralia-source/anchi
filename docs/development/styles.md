# Guía visual de Anchi

Esta guía recoge el lenguaje visual que ya utiliza el panel operativo de Anchi y sirve como referencia para nuevas pantallas, especialmente las pantallas de acceso y configuración. La regla principal es reutilizar los tokens y los componentes existentes antes de crear estilos aislados.

## 1. Principios de composición

- Interfaz de aplicación de escritorio dentro del navegador: sidebar persistente, contenido principal flexible y cabecera de contexto.
- Jerarquía compacta y funcional: títulos claros, textos auxiliares discretos y controles alineados en filas.
- Superficies planas con separación por borde; las sombras se reservan para overlays, cajones y elementos que flotan.
- El color primario identifica acciones y navegación activa; no se utiliza como decoración indiscriminada.
- Las pantallas deben respirar: evitar cajas anidadas innecesarias y agrupar contenido por secciones, no por acumulación de tarjetas.

## 2. Tipografía

La fuente se resuelve mediante `--font-family`, configurada por identidad y con esta pila de respaldo:

```css
Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
"Segoe UI", sans-serif
```

- Texto base: 12px en el panel operativo, con `line-height: 1.3`.
- Título de página: 21px, peso 700, interlineado aproximado de 1.15.
- Títulos de sección: 16–18px, peso 700–800.
- Etiquetas de formulario y metadatos: 11–12px, color muted.
- Acciones: 12–14px, peso 600–700.
- No usar mayúsculas completas salvo en pequeños labels de contexto; si se usan, añadir espaciado entre letras y reducir el tamaño.

## 3. Color

Los valores se generan en `branding_css_vars()` para que la identidad de cada empresa se aplique globalmente. Estos son los valores por defecto actuales:

| Token | Valor | Uso |
|---|---|---|
| `--bg` | `#F5F7F6` / `#F5F7F8` | Fondo general de la aplicación |
| `--panel` | `#FFFFFF` | Superficies principales |
| `--text` | `#1B1F22` / `#172026` | Texto principal |
| `--muted` | `#5F6B73` / `#63717B` | Texto secundario, ayuda y metadatos |
| `--line` | `#DDE5E2` / `#DBE2E6` | Bordes y separadores |
| `--accent` | `#123A32` | Acción primaria, sidebar y estados activos |
| `--accent-dark` | `#0B2924` | Hover/pressed de la acción primaria |
| `--danger` | `#D61F2C` / `#B91C1C` | Errores y acciones destructivas |

La identidad admite también fondos y textos específicos de sidebar, botones, tablas, scoring y estados. El hover de una acción primaria debe ser una versión atenuada del primario, nunca un color azul o gris arbitrario.

## 4. Layout y espaciado

- Sidebar operativo: normalmente `224px`, con versión contraída de `68px`.
- Padding del contenido principal: `14px 16px` en el layout compacto actual.
- Cabecera: separación inferior mediante `border-bottom: 1px solid var(--line)` y margen inferior corto (`10px`).
- Gaps habituales: 4px para navegación, 8px para controles compactos, 10–12px para formularios y grids, 14–16px para secciones.
- Altura de controles pequeños: alrededor de `30px`; cabecera y acciones superiores: `34px`.
- En móvil, el sidebar pasa a flujo normal y las composiciones de dos columnas se apilan.

## 5. Bordes, radios y sombras

- Borde estándar: `1px solid var(--line)`.
- Radio de botón configurable: normalmente 5–8px.
- Radio de panel compacto: 7–8px; las tarjetas de identidad admiten hasta 14px cuando actúan como superficie principal.
- Badges y estados: `border-radius: 999px`.
- Sombra de tarjetas: muy ligera o inexistente en vistas densas.
- Sombra visible solo para menús, drawers y overlays: amplia, suave y con baja opacidad.
- El foco debe ser visible con borde primario y/o un halo suave; nunca eliminar `:focus-visible`.

## 6. Cabecera estándar

La cabecera del panel utiliza:

- Título de página a la izquierda.
- Acciones agrupadas a la derecha, con controles de `34px` de altura.
- Separadores verticales de 1px entre grupos.
- Botón de alertas con icono de campana y badge circular.
- Botón “Nueva entrada” con icono `plus`.
- Contexto de empresa en una cápsula discreta.
- Usuario y rol en dos líneas, con acción de salida basada en icono.

Las pantallas públicas, como el acceso, deben conservar esta proporción, borde inferior y lenguaje de controles aunque no muestren información de usuario autenticado.

## 7. Sidebar y navegación

- Fondo `var(--accent)` y texto claro.
- Marca arriba, con logo opcional y nombre de aplicación.
- Enlaces de navegación de `36px` de alto, gap de `4px`, radio de `8px` y padding horizontal de `10px`.
- Iconos de `20–22px`, alineados en una columna fija.
- Hover: mezcla ligera del blanco con el primario.
- Activo: fondo blanco, texto primario y sombra mínima.
- Iconos siempre inline SVG de `templates/components/icons.html`, con `currentColor`, viewBox `24x24`, trazo redondeado de aproximadamente 2px.

## 8. Iconografía

La fuente de iconos de Anchi es el conjunto de macros SVG local, con estética Lucide/Heroicons. No añadir emojis, caracteres Unicode ni otra librería para acciones de interfaz.

```jinja2
{% from "components/icons.html" import icon_mail, icon_lock, icon_shield %}
{{ icon_mail(20) }}
```

Cada icono debe:

- Heredar el color mediante `currentColor`.
- Tener `aria-hidden="true"` cuando acompaña un texto visible.
- Mantener trazo, viewBox y proporción del resto del sistema.
- Usar un `aria-label` o `title` en botones que solo contengan el icono.

## 9. Botones y formularios

- Primario: fondo `var(--accent)`, texto claro, radio de botón, altura mínima de 30px y hover `var(--accent-dark)` o mezcla atenuada.
- Secundario: fondo blanco, texto `var(--button-secondary-text)`, borde estándar y hover con fondo gris muy claro.
- Peligro: rojo reservado para acciones destructivas o errores.
- Inputs: fondo blanco, borde estándar, radio de botón y padding horizontal de 8–10px.
- Labels: encima del control, compactos, con color muted y separación de 3–4px.
- Errores: deben ocupar una franja legible, con color de error y fondo muy tenue; no depender solo del color.
- Estados de carga: mantener el botón visible, desactivarlo mientras procesa y mostrar el texto o icono de carga.

## 10. Estados y accesibilidad

- Activo, hover, foco, deshabilitado y error deben diferenciarse de forma consistente.
- El estado activo de navegación combina fondo y color, no solo color.
- Los textos secundarios deben conservar contraste suficiente sobre `--bg` y `--panel`.
- Las áreas táctiles de botones e iconos no deben ser menores de aproximadamente 30–36px.
- El responsive no debe crear scroll horizontal salvo en tablas que lo necesiten.

## 11. Aplicación al panel Superadmin

El panel Superadmin debe utilizar el mismo shell visual que el espacio de trabajo: sidebar de marca y navegación, cabecera de contexto, fondo `--bg`, superficies planas, tablas densas, controles estándar e iconos SVG locales. Su navegación y permisos siguen siendo independientes del área operativa de cada empresa.

La navegación del sidebar de Superadmin es propia del plano de plataforma y no sustituye la autorización: las acciones siguen requiriendo la sesión y el rol adecuados.
