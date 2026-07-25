---
name: AI Broker
description: Sala de control en penumbra para un gateway de inferencia multi-LLM privado
colors:
  bg: "#07111d"
  sidebar: "#091522"
  surface: "#0c1927"
  well: "#081522"
  elevated: "#0d1b29"
  raised: "#102131"
  track: "#1b2d3d"
  border: "#223447"
  border-soft: "#182a3b"
  border-strong: "#3a5a78"
  muted-line: "#596b7d"
  text: "#eef5fb"
  text-2: "#dce6ef"
  text-3: "#cbd8e3"
  text-4: "#b7c6d3"
  muted: "#91a3b5"
  teal: "#31c6ae"
  on-teal: "#041817"
  teal-fill: "#0c2320"
  teal-ink: "#9fe8dd"
  blue: "#59aef7"
  green: "#4fd071"
  green-fill: "#10291c"
  green-line: "#25623d"
  green-ink: "#72dc8c"
  amber: "#f5ad31"
  amber-fill: "#211b0d"
  amber-line: "#6b511f"
  amber-ink: "#ffd37a"
  red: "#f06a6a"
  red-fill: "#2a171e"
  red-line: "#6d343b"
  red-ink: "#ff9ca4"
  info-fill: "#0c1e30"
  info-line: "#244b6d"
  info-ink: "#9fc9f0"
  topbar-bg: "rgba(7, 17, 29, .96)"
  backdrop: "rgba(3, 9, 15, .62)"
typography:
  display:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "1.5625rem"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: "tabular-nums"
  headline:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "1.375rem"
    fontWeight: 720
    lineHeight: 1.45
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 680
    lineHeight: 1.45
  title-sm:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 680
    lineHeight: 1.45
  control:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 650
    lineHeight: 1.45
  read:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.6
  body:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.45
  code:
    fontFamily: "ui-monospace, Cascadia Code, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  control: "4px"
  surface: "5px"
  overlay: "6px"
  track: "8px"
  pill: "999px"
spacing:
  tight: "8px"
  grid: "12px"
  panel-gap: "14px"
  panel-pad: "16px"
  shell-x: "22px"
components:
  button-primary:
    backgroundColor: "{colors.teal}"
    textColor: "{colors.on-teal}"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "36px"
    typography: "{typography.control}"
  button-secondary:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text-3}"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "36px"
    typography: "{typography.control}"
  button-danger:
    backgroundColor: "{colors.red-fill}"
    textColor: "{colors.red-ink}"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "36px"
    typography: "{typography.control}"
  input-field:
    backgroundColor: "{colors.well}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "9px 10px"
    typography: "{typography.body}"
  badge:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text-3}"
    rounded: "{rounded.control}"
    padding: "0 8px"
    height: "23px"
    typography: "{typography.label}"
  badge-ok:
    backgroundColor: "{colors.green-fill}"
    textColor: "{colors.green-ink}"
    rounded: "{rounded.control}"
    padding: "0 8px"
    height: "23px"
  badge-warn:
    backgroundColor: "{colors.amber-fill}"
    textColor: "{colors.amber-ink}"
    rounded: "{rounded.control}"
    padding: "0 8px"
    height: "23px"
  badge-fail:
    backgroundColor: "{colors.red-fill}"
    textColor: "{colors.red-ink}"
    rounded: "{rounded.control}"
    padding: "0 8px"
    height: "23px"
  filter-chip:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text-3}"
    rounded: "{rounded.pill}"
    padding: "5px 12px"
    typography: "{typography.label}"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.surface}"
  metric-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.surface}"
    padding: "17px 18px"
    height: "116px"
  nav-item:
    backgroundColor: "{colors.sidebar}"
    textColor: "{colors.text-4}"
    padding: "0 22px 0 26px"
    height: "48px"
  nav-item-active:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
---

# Design System: AI Broker

## Overview

**Creative North Star: "La sala de control"**

Un panel de instrumentos en penumbra, vigilado de reojo desde un segundo monitor. El chasis desaparece; las señales mandan. Todo lo que no es dato — el fondo, los bordes, los contenedores — se retira a la penumbra hasta volverse invisible, y lo poco que se ilumina se ilumina porque algo está ocurriendo. Un operador que levanta la vista durante dos segundos debe saber si hay algo que atender antes de haber leído una sola palabra.

De ahí viene la economía cromática del sistema. Sobre un azul marino casi negro se escalonan tres superficies apenas distinguibles, y sobre ellas vive **un único acento**: el Verde Osciloscopio. No es un color de marca, es una lectura de instrumento — marca lo activo, lo que avanza, lo que está en curso. A su lado, tres colores de estado (verde, ámbar, rojo) y un azul de traza para lo navegable. Nada más. Un sistema con un solo acento no necesita jerarquía de acentos: necesita disciplina para no gastarlo.

La segunda ley del sistema es que **toda cifra llega con su procedencia**. No hay métricas simuladas, no hay barras de progreso estimadas, no hay porcentajes decorativos. Cuando un dato falta se escribe `N/D` con su motivo, y cuando existe llega con su ventana temporal, su denominador y su hora de comprobación. Esta no es una decisión estética, pero gobierna la estética: es la razón de que la numérica sea tabular, de que las tarjetas de métrica reserven una línea entera para el contexto bajo la cifra, y de que el sistema no tenga un solo gráfico decorativo.

Se rechazan explícitamente tres vecindarios visuales: los **dashboards de cripto/trading** (neón saturado, cifras gritando, verde y rojo permanentes), la **estética SaaS genérica** (degradados violeta-azul, tarjetas flotantes, esquinas muy redondeadas, ilustraciones isométricas) y el **terminal retro/hacker** (todo monoespaciado, fósforo sobre negro puro, scanlines). El tercero es el más peligroso porque roza la metáfora: aquí el instrumental es función, nunca disfraz.

**Key Characteristics:**

- Fondo azul marino profundo, **nunca negro puro**; texto casi blanco, **nunca blanco puro**.
- Un solo acento en todo el sistema, más tres colores de estado y un azul de traza.
- Profundidad por escalonado tonal; la sombra solo susurra.
- Sin degradados, sin brillos, sin iconografía decorativa, sin imágenes.
- Numérica tabular en toda cifra comparable.
- Tipografía del sistema operativo: cero recursos externos, cero CDN.
- Densidad alta pero subordinada: cuando legibilidad y densidad compiten, gana la legibilidad.

## Colors

Una penumbra azul marino de la que emerge un solo instrumento encendido.

Cada color del sistema es un token `--*` en `:root`. **No existe un solo literal de color fuera de ese bloque**, y el nombre del token es la unidad de vocabulario: se dice `var(--red-ink)`, no «el rojo claro». Las familias de estado se declinan siempre igual — un tono puro para el punto, y una tríada `-fill` / `-line` / `-ink` para insignias, alertas y bloques.

### Primary

- **Verde Osciloscopio** `--teal` (`#31c6ae`): el único acento del sistema. Marca lo que está vivo o en curso: borde izquierdo de la navegación activa, relleno del botón primario, relleno de medidores y barras de progreso, carril de proponente en la comparación temporal, borde del chip de filtro activo. No se usa para texto de párrafo, no se usa como fondo de superficie amplia, y no se usa para señalar éxito — eso es competencia de Verde Confirmación.
- **Tinta Osciloscopio** `--on-teal` (`#041817`): el único texto que se escribe sobre el acento. Un verde tan oscuro que lee como negro, pero pertenece a la familia del acento en vez de agujerearlo.
- **Relleno y Tinta de Acento** `--teal-fill` (`#0c2320`) · `--teal-ink` (`#9fe8dd`): la única superficie teñida de acento — el chip de filtro activo. Un filtro encendido cambia lo que estás mirando, y eso es una noticia.

### Secondary

- **Azul de Traza** `--blue` (`#59aef7`): todo lo navegable y todo lo enfocado. Enlaces de tarea (en monoespaciado), botones-enlace, el anillo de foco de cualquier control, y el carril del árbitro en la comparación — donde se opone deliberadamente al acento para distinguir árbitro de proponente. Es el segundo color más frecuente del sistema y aun así nunca compite con el acento, porque solo aparece sobre texto y contornos, jamás como relleno.

### Tertiary

Cuatro familias declinadas idénticamente. Tres son estado; la cuarta, `info`, no lo es — viste lo explicativo (la alerta informativa y el estado `error` de un modelo, que significa «fallo temporal, se reintenta», no «vetado»).

- **Verde Confirmación** `--green` (`#4fd071`) · `--green-fill` (`#10291c`) · `--green-line` (`#25623d`) · `--green-ink` (`#72dc8c`): sano, completado, activo, compatible.
- **Ámbar de Aviso** `--amber` (`#f5ad31`) · `--amber-fill` (`#211b0d`) · `--amber-line` (`#6b511f`) · `--amber-ink` (`#ffd37a`): degradado, en cola, desconocido, y el banner de conexión perdida.
- **Rojo de Fallo** `--red` (`#f06a6a`) · `--red-fill` (`#2a171e`) · `--red-line` (`#6d343b`) · `--red-ink` (`#ff9ca4`): no disponible, fallido, incompatible, destructivo.
- **Azul de Información** `--info-fill` (`#0c1e30`) · `--info-line` (`#244b6d`) · `--info-ink` (`#9fc9f0`): la alerta informativa, la insignia neutra y el estado `error` reintentable. No tiene tono puro porque no hay punto de estado «informativo».

### Neutral

**Superficies** — cinco escalones de un mismo azul marino, deliberadamente juntos:

- **Abismo** `--bg` (`#07111d`): el fondo del mundo. Es el color que más superficie ocupa y el que nunca se mira.
- **Chasis** `--sidebar` (`#091522`): la barra lateral. Un escalón por encima del abismo, lo justo para separar navegación de contenido sin una línea.
- **Panel** `--surface` (`#0c1927`): toda superficie que contiene datos — tarjetas, paneles, tablas.
- **Realzado** `--elevated` (`#0d1b29`): el escalón intermedio. Cabecera de tabla y hover de navegación: algo que se distingue de su contenedor sin llegar a activarse.
- **Realce** `--raised` (`#102131`): el estado activo o seleccionado de un elemento interactivo. Navegación activa, botón secundario, chip en reposo, fila de tabla bajo el cursor, tarjeta seleccionada, insignia, toast.
- **Pozo** `--well` (`#081522`): el interior de un campo de entrada y de todo bloque de código o resultado. Más oscuro que el panel que lo contiene: los campos se hunden, no se elevan.
- **Pista** `--track` (`#1b2d3d`): el canal vacío de un medidor, una barra de progreso o un carril temporal. Es siempre el fondo de algo que se rellena.

**Líneas** — cuatro, cada una con una función distinta:

- **Filo** `--border` (`#223447`): separa regiones (panel del fondo, cabecera de cuerpo).
- **Filo Suave** `--border-soft` (`#182a3b`): separa hermanos dentro de una misma región (filas de tabla, filas de lista).
- **Filo Vivo** `--border-strong` (`#3a5a78`): el borde de un control interactivo bajo el cursor.
- **Filo Inerte** `--muted-line` (`#596b7d`): marca lo que no está pasando. Borde de una tarea cancelada y carril temporal sin medida — dos elementos distintos que significan lo mismo.

**Texto** — el principal más una rampa de cuatro pasos, y nada por debajo:

- **Blanco de Instrumento** `--text` (`#eef5fb`): texto principal, títulos, valores destacados. Blanco con sesgo azul, nunca `#fff`.
- **Texto Enfático** `--text-2` (`#dce6ef`): el contenido que importa dentro de un bloque secundario — el `strong` de una lista, el cuerpo de una alerta, la métrica de un carril.
- **Texto Corriente** `--text-3` (`#cbd8e3`): celdas de tabla, etiquetas de control, botón secundario, chip, casillas.
- **Texto Técnico** `--text-4` (`#b7c6d3`): bloques de código y previsualización, insignias, navegación en reposo, campos de formulario.
- **Acero Apagado** `--muted` (`#91a3b5`): etiquetas, unidades, marcas de tiempo, cabeceras de tabla, texto de apoyo. **Es el suelo cromático del sistema: ningún texto es más tenue que esto.**

### Named Rules

**La Regla de la Voz Única.** El Verde Osciloscopio es el único acento del sistema y no admite compañía. Antes de introducir un color nuevo, la pregunta correcta no es «¿cuál pega?» sino «¿por qué esto no es estado, no es traza y no es acento?». Si no hay respuesta, no hay color nuevo.

**La Regla de Ningún Extremo.** Ni `#000` ni `#fff` aparecen jamás en este sistema. El fondo más oscuro es el Abismo y el texto más claro es el Blanco de Instrumento. El contraste extremo sostenido fatiga en sesiones largas, y este panel está abierto todo el día.

**La Regla del Estado Ganado.** Verde, ámbar y rojo son vocabulario de estado, no paleta decorativa. Un elemento se pinta de rojo porque el sistema ha observado un fallo, nunca porque la acción «suene peligrosa». El color se gana con un hecho.

**La Regla de la Doble Señal.** Ningún estado se comunica solo por color. Todo punto de estado va acompañado de su etiqueta textual, y toda insignia lleva la palabra además del tono. Una captura en escala de grises dentro de una memoria impresa debe seguir siendo legible.

**La Regla del Cero Literal.** Fuera de `:root` no existe ni un valor de color escrito a mano. Ni un hex, ni un `rgba`. Test mecánico: `grep -oE '#[0-9a-fA-F]{3,6}|rgba\(' app/static/dashboard.css` no debe devolver nada por debajo del bloque de tokens. Un literal suelto no es un atajo, es el primer paso de una familia de seis rojos casi iguales.

**La Regla de la Tríada Completa.** Un estado se viste con sus tres piezas o con ninguna: `-fill` para el fondo, `-line` para el borde, `-ink` para el texto. Media tríada — un texto rojo sin su relleno y su borde — produce un elemento que grita sin decir qué es.

**La Regla del Suelo Cromático.** `--muted` es el texto más tenue que el sistema admite. Si un elemento parece necesitar algo más apagado, el problema es que sobra en la pantalla, no que le falte gris.

## Typography

**Display / Body Font:** Segoe UI Variable (con Segoe UI, system-ui, sans-serif)
**Label/Mono Font:** ui-monospace (con Cascadia Code, monospace)

**Character:** La tipografía del sistema operativo, sin una sola familia descargada. Segoe UI Variable aporta un eje de peso continuo que el sistema explota con pesos poco convencionales — 560, 650, 680, 720 — en lugar de saltar de 400 a 700. Ese matiz es la firma tipográfica del panel: la jerarquía se construye con incrementos de peso pequeños y deliberados, no con contraste bruto. El monoespaciado está reservado a lo que es literalmente un identificador o un texto que viajó: IDs de tarea, prompts, salidas crudas, resultados de herramientas.

### Hierarchy

Toda la escala se expresa en `rem` sobre el tamaño de fuente por defecto del navegador. Los equivalentes en px que siguen valen para el ajuste de fábrica (16px); si el usuario sube el tamaño de fuente del navegador, toda la escala sube con él.

- **Display** (700, `1.5625rem` / 25px, line-height 1, letter-spacing −0.02em, numérica tabular): la cifra de una tarjeta de métrica. Es el único tamaño grande del sistema y existe para ser leído desde lejos.
- **Headline** (720, `1.375rem` / 22px, letter-spacing −0.02em): exclusivamente el logotipo textual «AI Broker» en la barra lateral. No se reutiliza.
- **Title** (680, `1rem` / 16px): encabezado de panel (`h2`). Un panel, un título.
- **Title-sm** (680, `0.875rem` / 14px): encabezado de subsección dentro de un panel (`h3`), como los bloques de línea temporal o los subgrupos de configuración.
- **Control** (650, `1rem` / 16px): el texto de un botón. Deliberadamente mayor que el cuerpo: un control es una diana, y su etiqueta debe leerse sin buscarla. Los botones compactos bajan a Body, nunca a Label.
- **Read** (400, `0.9375rem` / 15px, line-height 1.6, medida máxima 74ch): la única superficie de lectura larga del panel — la respuesta del modelo y los prompts en el detalle de tarea. Es el sitio donde el operador lee párrafos enteros en vez de escanear datos, y por eso es el único rol que rompe la densidad del sistema.
- **Body** (400, `0.8125rem` / 13px, line-height 1.45): texto general — celdas de tabla, campos, listas de detalle, párrafos de alerta. Las cabeceras de tabla usan este tamaño con peso 560.
- **Label** (400, `0.75rem` / 12px, line-height 1.45): unidades, marcas de tiempo, contexto bajo una cifra, pistas de formulario, texto de apoyo. **Es el suelo del sistema: nada baja de aquí.**
- **Code** (400, `0.8125rem` / 13px, line-height 1.55, monoespaciado): IDs de tarea, prompts, salidas del modelo, resultados de herramientas, bloques de configuración.

### Named Rules

**La Regla del Suelo de 12px.** Ningún texto del sistema baja de 12px, en ninguna pantalla, en ningún tamaño de ventana. Test: si un valor de `font-size` es menor que `12px`, es un error, no una decisión de densidad.

**La Regla de la Cifra Tabular.** Toda cifra que un ojo pueda querer comparar con otra — latencias, costes, contadores, posiciones de cola, porcentajes — se escribe con `font-variant-numeric: tabular-nums`. Las columnas de números deben alinearse por dígito o no son columnas.

**La Regla del Peso Fino.** La jerarquía se construye con incrementos de peso de la fuente variable (560 → 650 → 680 → 720), no con mayúsculas, no con letra espaciada y no con color. Si dos elementos necesitan distinguirse, la primera herramienta es el peso.

**La Regla del Texto Escalable.** Todo tamaño de texto se declara en `rem`, nunca en px. Un `font-size` en píxeles ignora el ajuste de tamaño de fuente del navegador, y ese ajuste es la única herramienta de accesibilidad que un usuario puede aplicar sin tocar el código. Los px siguen siendo correctos para el armazón, los bordes, los radios y las alturas de control: solo el texto necesita escalar. Test: si un valor de `font-size` está en px, es un error.

**La Regla de la Densidad Subordinada.** El cuerpo del panel es denso (13px) porque es un instrumento de datos, pero la densidad no gobierna donde se lee de verdad. La superficie de lectura larga sube a Read y se limita a 74 caracteres de medida. Cuando densidad y legibilidad compiten, gana la legibilidad.

## Layout

**El armazón.** Rejilla de dos columnas a pantalla completa: barra lateral fija de **190px** y espacio de trabajo fluido. La barra lateral es `sticky` a altura de ventana completa; la barra superior es `sticky` dentro del espacio de trabajo, de modo que el estado del servicio nunca se pierde al desplazarse. Ambas barras miden **62px** de alto y comparten línea de base — la marca y el estado del servicio se leen como una sola franja.

**El contenido** se limita a **1680px** y se centra. Es un techo alto a propósito: el panel vive en un monitor ancho y la rejilla de métricas necesita cinco columnas para que la fila de cabecera se lea de un vistazo.

**Ritmo espacial.** El sistema trabaja con cinco medidas de carga: 8px para separaciones internas, 12px entre tarjetas de métrica, **14px como separación canónica entre paneles**, 16px como relleno interno de panel, y 22px de margen lateral del armazón. Los paneles nunca llevan relleno propio en su raíz: cada región interna (cabecera, nota, cuerpo, acciones) trae el suyo, lo que permite que tablas y listas lleguen a sangre hasta el borde del panel.

**Composición dominante.** Rejilla asimétrica de dos columnas con el lado principal más ancho: `1.55fr / 0.95fr` en el resumen, `1.2fr / 0.8fr` en el probador, `1fr / 340px` en el detalle de tarea. La columna ancha es siempre la del artefacto que se está examinando; la estrecha, la del contexto que lo acompaña.

**Comportamiento responsive.** Dos puntos de corte, ambos por colapso, ninguno por reordenación arbitraria:

- **≤1150px** — todas las rejillas de dos columnas se apilan a una. La rejilla de métricas pasa de 5 a 3 columnas. Se conserva el armazón lateral.
- **≤760px** — el armazón se desmonta: la barra lateral se convierte en una tira horizontal desplazable sobre el contenido, y el indicador de navegación activa migra del borde izquierdo al borde inferior. Las métricas caen a 2 columnas y las filas de salud y de línea temporal se reflowean a una columna.

### Named Rules

**La Regla del Panel Sangrado.** Un panel es un contenedor sin relleno. Las tablas y las listas llegan hasta su borde y se separan entre sí con Filo Suave; el aire lo ponen las regiones internas. Un panel con `padding` en la raíz rompe la continuidad de la tabla que contiene.

**La Regla de las Dos Barras.** Barra lateral y barra superior miden 62px y no cambian de altura. Son el marco del instrumento: si el marco se mueve al navegar, el ojo periférico interpreta movimiento donde no hay noticia.

## Elevation & Depth

**El tono manda; la sombra susurra.** La profundidad de este sistema la construye un escalonado tonal de cuatro pasos — Abismo `#07111d` → Chasis `#091522` → Panel `#0c1927` → Realce `#102131` — con saltos deliberadamente pequeños. La sombra ambiental que llevan tarjetas y paneles existe solo para despegarlos del fondo lo justo; sobre un fondo tan oscuro es casi imperceptible, y así debe seguir. Los campos de entrada invierten el gesto: se hunden hacia el Pozo `#081522` en vez de elevarse.

Las sombras fuertes están reservadas a lo que **flota de verdad**: lo que se superpone al plano del documento en lugar de pertenecer a él. Ahí la sombra sí es estructural, porque es la única señal de que el elemento no está en la página.

### Shadow Vocabulary

- **Reposo** (`box-shadow: 0 8px 24px rgba(0, 0, 0, .13)`): toda tarjeta de métrica y todo panel. Despega la superficie del fondo. No cambia con el hover.
- **Flotante** (`box-shadow: 0 12px 30px rgba(0, 0, 0, .3)`): elementos superpuestos y efímeros — toast de acción y banner de conexión.
- **Modal** (`box-shadow: 0 18px 44px rgba(0, 0, 0, .45)`): el diálogo de confirmación, acompañado de un velo `rgba(3, 9, 15, .62)`. Es la única sombra del sistema que se percibe con claridad, y lo es porque interrumpe.

### Named Rules

**La Regla de la Sombra Quieta.** Las sombras no responden al hover. Ninguna superficie se eleva al pasar el cursor: la respuesta al hover es tonal (fondo un escalón más claro), nunca de altura. Un panel que sube al pasar por encima es movimiento sin noticia, y este panel se vigila de reojo.

## Shapes

**Geometría sobria, casi ortogonal.** El radio dominante es de 4-5px: lo justo para que un borde no corte, muy lejos de la esquina blanda del SaaS genérico. No hay formas orgánicas, no hay recortes, no hay silueta decorativa. El único elemento plenamente redondeado del sistema es el chip de filtro, y su forma de píldora es funcional: distingue de un vistazo el control de filtrado de cualquier botón de acción.

Los radios se asignan **por clase de elemento**, no por gusto:

- **Control** (4px): botones, insignias, campos, alertas, toast, banner. Todo lo que se pulsa o se escribe.
- **Superficie** (5px): paneles, tarjetas de métrica, tarjetas de catálogo, tarjetas de proveedor. Es el token `--radius` del proyecto.
- **Superposición** (6px): diálogo modal y bloques de resultado embebidos.
- **Carril** (8px): barras de progreso, medidores y carriles de línea temporal — pistas que contienen un relleno móvil y necesitan que el relleno case con la pista.
- **Píldora** (999px): exclusivamente el chip de filtro.

**Las líneas son de 1px y solo hay dos.** Filo para separar regiones, Filo Suave para separar hermanos. No existen bordes de 2px salvo el indicador de navegación activa (3px a la izquierda en escritorio, 2px abajo en móvil), que es deliberadamente más grueso porque es la única marca de orientación del panel.

### Named Rules

**La Regla de los Cinco Radios.** El sistema tiene exactamente cinco radios y ninguna pantalla nueva introduce un sexto. Test: si un valor de `border-radius` no es 4px, 5px, 6px, 8px o 999px, está mal. *(Nota honesta: la convivencia de 4px, 5px y 6px es herencia de la implementación, no una decisión deliberada. Está congelada aquí para que la deriva pare, no porque tres escalones vecinos sean lo ideal.)*

## Components

Carácter general: **sobrios y de servicio**. Radios pequeños, un borde de 1px, sin relleno superfluo, sin gradiente y sin brillo. El control no compite nunca con el dato que muestra.

### Buttons

- **Shape:** esquinas apenas suavizadas (4px), altura mínima 36px, relleno horizontal de 13px.
- **Primary:** relleno Verde Osciloscopio con Tinta Osciloscopio encima (`#31c6ae` sobre `#041817`), peso 650. Es la única superficie amplia del sistema que lleva el acento, y por eso hay como mucho una por región.
- **Secondary:** fondo Realce, borde Filo, texto Texto Atenuado. La opción por defecto para todo lo que no es la acción principal.
- **Danger:** tríada de fallo completa — relleno `#2a171e`, borde `#6d343b`, tinta `#ff9ca4`. Nunca rojo pleno: un botón destructivo debe leerse como advertencia, no como alarma disparada.
- **Compact:** variante de 30px de alto para acciones dentro de filas y cabeceras de panel.
- **Hover:** `filter: brightness(1.08)`. Un solo mecanismo para todas las variantes, sin desplazamiento y sin cambio de sombra.
- **Focus:** anillo de 2px en Azul de Traza con 2px de separación (`outline-offset`). Idéntico en botones, enlaces y botones-enlace.

### Chips

- **Style:** píldora completa (999px), fondo Realce, borde Filo, texto Texto Atenuado.
- **Hover:** solo cambia el borde, a `#3a5a78`. El fondo no se mueve.
- **Active:** borde Verde Osciloscopio, fondo `#0c2320` y texto `#9fe8dd`. Es uno de los pocos lugares donde el acento aparece en un control secundario, porque un filtro activo cambia lo que se está mirando y eso es una noticia.

### Cards / Containers

- **Corner Style:** 5px.
- **Background:** Panel `#0c1927` sobre fondo Abismo.
- **Shadow Strategy:** sombra de Reposo (ver Elevation & Depth). Constante, no reactiva.
- **Border:** 1px Filo.
- **Internal Padding:** la raíz no lleva; 16px en cabecera, nota y acciones. La tarjeta de métrica es la excepción, con 17-18px y altura mínima de 116px para que la cifra tenga aire propio.
- **Panel heading:** 58px de alto mínimo, título a la izquierda y acciones a la derecha, separado del cuerpo por Filo.

### Inputs / Fields

- **Style:** fondo Pozo `#081522` — más oscuro que el panel que los contiene —, borde 1px Filo, radio 4px, relleno 9px/10px. Etiqueta encima en Acero Apagado.
- **Focus:** anillo de 2px en Azul de Traza con 1px de separación. El borde no cambia; el anillo se añade.
- **Error:** el borde pasa a `#6d343b` y aparece una pista bajo el campo en `#ff9ca4`. El mensaje va siempre debajo, nunca como sustituto del valor.
- **Textarea:** monoespaciado, altura mínima 90px, redimensionable solo en vertical.

### Navigation

- **Style:** lista vertical en el Chasis, 48px por elemento, sin iconos. Solo palabras.
- **Default:** texto `#bac8d5` sobre Chasis, con un borde izquierdo transparente de 3px reservado.
- **Hover:** fondo `#0d1c2b`, texto a Blanco de Instrumento.
- **Active:** fondo Realce y el borde izquierdo de 3px encendido en Verde Osciloscopio. **Es el único uso del acento en la barra lateral**, y su función es orientar: en un panel de ocho pantallas, saber dónde estás debe costar cero.
- **Mobile (≤760px):** tira horizontal desplazable; el indicador migra a un borde inferior de 2px.

### Insignias de estado

El componente más repetido del panel y el que carga la mayor parte del significado. Altura fija de 23px, radio 4px, peso 650, sin ajuste de línea. Cada estado es una tríada completa relleno/borde/tinta, nunca solo un color de texto. Cubre estados de tarea (en cola, activa, completada, fallida, cancelada), de salud (sana, degradada, no disponible) y de compatibilidad de modelo (compatible, incompatible, desconocido, error).

### Carril temporal *(componente de firma)*

La representación de un mixture en la pantalla de Comparación: una pista de 12px con radio de carril, fondo `#1b2d3d`, y una barra posicionada en absoluto cuyo desplazamiento y anchura son **tiempo real medido**, no proporción decorativa. Verde Osciloscopio para los proponentes, Azul de Traza para el árbitro — la misma oposición acento/traza que rige el resto del sistema, aquí puesta a trabajar como leyenda.

Su detalle más importante es el estado `untimed`: cuando faltan las marcas de tiempo por invocación, la barra ocupa el carril entero en gris `#596b7d`. El sistema **declara la ausencia de medida** en lugar de dibujar una duración plausible. Es la Regla de la Ausencia Declarada hecha componente.

### Progreso indeterminado

Cuando el broker está esperando a un modelo no existe porcentaje real que mostrar — los proveedores actuales usan `stream=false` y devuelven las cifras al terminar. El sistema responde con una banda del 34% que recorre una pista de 5px en bucle (`2.6s ease-in-out alternate`). Es la única animación continua del panel y es honesta: comunica «trabajando» sin afirmar cuánto queda.

Tres decisiones la mantienen tolerable en un panel que vive abierto en un segundo monitor:

- **Va lenta a propósito.** La salencia periférica crece con la velocidad, no con el brillo. El recorrido se frena a 2.6s y el color se mantiene a pleno teal: fácil de leer cuando la miras, difícil de que te robe la mirada cuando no.
- **No reinicia.** El fragmento de tarea activa se reemplaza entero cada 3s, y una animación CSS arrancaría desde el fotograma cero en cada swap — un tirón periódico, que es justo lo que el ojo periférico detecta mejor. `dashboard.js` aplica un `animation-delay` negativo calculado sobre un reloj continuo para que la banda siga donde estaba.
- **Se para cuando nadie la ve.** Un `IntersectionObserver` pone la animación en `paused` al salir del viewport y la resincroniza al volver.

### Named Rules

**La Regla del Movimiento Provocado.** El movimiento que provoca el usuario (acuse de una acción, foco, hover) puede ser inmediato y visible: está mirando. El movimiento que provoca el sistema por su cuenta (progreso, auto-refresco, llegada de datos) se mantiene al mínimo que declare el estado: no está mirando. Test: si un elemento se mueve sin que el operador haya hecho nada, pregunta qué información se perdería si estuviera quieto. Si la respuesta es «ninguna», que esté quieto.

**La Regla del Refresco Invisible.** Los diez bloques que se auto-refrescan cada 3-30s cambian su contenido **sin transición**. Es deliberado: animar la llegada de datos convertiría el panel en un parpadeo permanente. Un dato nuevo aparece; no entra.

**La Regla del Anuncio por Cambio.** Los paneles que se auto-refrescan se reemplazan enteros, así que un `aria-live` puesto sobre ellos se destruye en cada swap y no llega a anunciar nada. La región viva es única, estable y visualmente oculta (`#live-region` en `base.html`), y `dashboard.js` escribe en ella **solo cuando el resumen de un panel cambia** — nunca en el primer render, nunca en un refresco que no trajo noticias. Un panel que se refresca cada 3s con un `aria-live` encima no es accesible: es un metrónomo. Los paneles declaran su resumen en `data-announce`.

**La Regla del Sustituto, no del Interruptor.** Con `prefers-reduced-motion: reduce` no se anula el movimiento con un `animation: 0.01ms` global — eso destruye señales útiles. Cada movimiento se sustituye por su equivalente inmediato: el toast conserva la opacidad y pierde el desplazamiento, el medidor salta al valor en vez de recorrerlo, y el desplazamiento suave pasa a instantáneo. La barra indeterminada es la única que desaparece del todo, y solo porque congelarla al 34% mentiría sobre un progreso que nadie ha medido: su estado ya viaja en la insignia, el campo Fase y el contador de invocaciones.

## Do's and Don'ts

### Do:

- **Do** acompañar toda cifra de su procedencia: ventana temporal, denominador y hora de comprobación. `Latencia p95` sin periodo declarado es un dato inválido en este sistema.
- **Do** escribir `N/D` con su motivo cuando falte un dato, y dejar el hueco visible.
- **Do** usar `font-variant-numeric: tabular-nums` en toda cifra comparable.
- **Do** dar a cada estado su tríada completa (relleno, borde, tinta) y su etiqueta textual. El color nunca viaja solo.
- **Do** escribir todo color como `var(--token)`. Si el color que necesitas no tiene token, la decisión es añadir un token al sistema, no un literal a la regla.
- **Do** mantener el acento por debajo del umbral de ruido: como mucho una superficie con Verde Osciloscopio por región visible.
- **Do** hundir los campos de entrada hacia el Pozo y elevar las superficies de datos al Panel. La dirección de la profundidad tiene significado.
- **Do** dejar que tablas y listas lleguen a sangre hasta el borde del panel.
- **Do** responder al hover con tono, y al foco con el anillo de 2px en Azul de Traza.
- **Do** dar a `prefers-reduced-motion: reduce` un sustituto inmediato para cada movimiento, no un interruptor global que apague el feedback.
- **Do** pausar cualquier bucle infinito cuando sale del viewport.
- **Do** declarar todo tamaño de texto con los tokens `--text-*` en `rem`, para que el ajuste de fuente del navegador surta efecto.
- **Do** limitar a 74ch cualquier superficie donde se lean párrafos enteros, y darle el rol Read.

### Don't:

- **Don't** usar `#000` ni `#fff` en ningún lugar del sistema.
- **Don't** introducir un segundo acento. El sistema tiene uno y su escasez es el mecanismo.
- **Don't** escribir un hex o un `rgba` fuera de `:root`, por pequeño o único que parezca el caso.
- **Don't** inventar un gris intermedio nuevo. La rampa tiene cinco pasos y son suficientes: si dudas entre dos, elige el más claro.
- **Don't** mostrar tokens, tokens/s ni porcentaje de generación en directo: los proveedores no los emiten hasta terminar. Durante una llamada individual la barra es indeterminada y así debe seguir.
- **Don't** dibujar un valor plausible donde falta una medida. Si no hay marcas de tiempo, el carril va gris y a lo ancho.
- **Don't** elevar superficies con la sombra al pasar el cursor. Las sombras de este sistema no reaccionan.
- **Don't** bajar de 12px (`0.75rem`) ningún texto, ni siquiera en una nota al pie o en una vista estrecha.
- **Don't** declarar un `font-size` en px. El armazón va en px; el texto, en `rem`.
- **Don't** introducir un sexto radio ni un tercer grosor de línea.
- **Don't** cargar una sola fuente, icono, hoja de estilos o script desde una red externa. La restricción la aplica la CSP, y es de privacidad: por este panel pasan prompts y documentos confidenciales.
- **Don't** usar degradados, brillos, ilustraciones isométricas, tarjetas flotantes de esquina blanda ni iconografía decorativa — el vecindario SaaS genérico está expresamente rechazado.
- **Don't** convertir el panel en un terminal: el monoespaciado se reserva a identificadores y a texto que viajó literalmente. El instrumental aquí es función, no disfraz.
- **Don't** animar `width`, `height`, `padding` ni `margin`. Para movimiento continuo, `transform`; para revelar, `grid-template-rows`.
- **Don't** animar la llegada de datos de un bloque que se auto-refresca. El contenido nuevo aparece; no entra.
- **Don't** añadir un segundo bucle infinito. El panel tiene uno y ya es el elemento más caro de su presupuesto de atención.
