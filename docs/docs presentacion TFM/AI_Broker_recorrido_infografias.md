# AI_Broker por dentro: la vida de una petición

Recorrido lineal del código de `C:\Procesos\AI_Broker`, contado en 12 infografías.
Cada capítulo trae tres cosas:

1. **Qué pasa en el código**, con las rutas y líneas donde verlo.
2. **Qué debe transmitir la infografía**, en una frase.
3. **El prompt** para generarla, listo para copiar.

Leído del código a 2026-09-24 (rama `feature/failover`, commit `e5813ebd`).

---

## Cómo usar los prompts

- **Pega primero el bloque de estilo común** y después el prompt del capítulo. Así las 12 imágenes salen como una serie: misma paleta, misma tipografía, mismo mapa de progreso abajo.
- **Las instrucciones están en inglés y los textos que deben aparecer en la imagen, en español y entre comillas.** Los generadores siguen mejor el diseño descrito en inglés, y las comillas les indican qué texto copiar literalmente.
- **Formato:** vertical 3:4 (por ejemplo 1536×2048). Si tu generador lo permite, pide la resolución más alta y luego escala con Magnific.
- **Si el texto sale deformado**, que es el fallo típico de estos modelos, usa la variante del final de cada prompt: pide la imagen sin texto y pon los rótulos después en un editor. En la sección "Textos exactos" de cada capítulo tienes el texto limpio para eso.
- **Colores fijos por capa**, iguales en toda la serie para que se reconozca de un vistazo en qué parte del sistema estás:

| Capa | Color | Hex |
|---|---|---|
| Entrada (API, validación) | Azul | `#1D4ED8` |
| Cola y despacho | Violeta | `#7C3AED` |
| Coordinador y estrategias | Naranja | `#C2410C` |
| Modelos y proveedores | Verde | `#047857` |
| Aprendizaje y evidencia | Magenta | `#BE185D` |
| Errores y cortes | Rojo | `#B91C1C` |

### Bloque de estilo común (pegar siempre delante)

```
STYLE (apply to the whole series): Clean editorial technical infographic, flat vector style, light warm off-white background (#F8F7F4), near-black text (#1A1A1A). Very large, highly legible sans-serif typography (like Inter or Segoe UI), minimum body text size equivalent to 24px at 1536px width, bold headings, generous spacing, high contrast (WCAG AA or better), no thin grey text, no tiny labels. Rounded rectangle cards with a thick colored left border, simple line icons, clear directional arrows. Layer color code used consistently: blue #1D4ED8 = API/entry, violet #7C3AED = queue/dispatcher, orange #C2410C = coordinator/strategies, green #047857 = models/providers, magenta #BE185D = learning/evidence, red #B91C1C = errors. Monospace font for file names. No photographs, no 3D, no gradients, no decorative clutter, no people. Vertical 3:4 layout. At the very top a small pill badge with the chapter number. At the very bottom a horizontal progress map of 12 small numbered circles connected by a line, labeled "Recorrido", with the current chapter circle filled and enlarged. All text in the image must be in Spanish, spelled exactly as given in quotes.
```

---

## Capítulo 01 · El mapa completo

**Qué pasa en el código.** El AI_Broker es un servidor FastAPI de un solo proceso (`scripts/run_broker.py`). Recibe peticiones de inferencia, las guarda en una cola SQLite y las atiende de una en una. Para cada una decide la estrategia (un modelo, un agente con herramientas o varios modelos que debaten) y el modelo concreto, entre locales (Ollama, LM Studio) y cloud. Lo elige por el **tiempo esperado hasta una respuesta buena**, medido con su propio historial. Todo lo que hace queda registrado y alimenta la siguiente decisión.

**Qué debe transmitir.** Una petición recorre 10 paradas en orden, y al final el broker aprende de ella.

```
Chapter badge: "01 · de 12". Title: "La vida de una petición en AI_Broker". Subtitle: "De la llamada HTTP a la respuesta, y lo que el broker aprende por el camino".

Main visual: a single winding path (like a metro line) running top to bottom through 10 numbered stations, each station a card with an icon and a short label, colored by layer:
1 (blue) "Arranque" – power icon
2 (blue) "Entrada: POST /api/v1/tasks" – door icon
3 (violet) "Cola en SQLite" – stacked list icon
4 (violet) "Despachador: una a la vez" – single-lane road icon
5 (orange) "Preparación" – checklist icon
6 (orange) "¿Qué estrategia?" – signpost with three arrows
7 (green) "¿Qué modelo?" – podium/ranking icon
8 (green) "La llamada al modelo" – chip icon
9 (orange) "Agente o debate" – two branches: robot with wrench, and three speech bubbles
10 (red/green split) "Desenlace" – flag icon
A curved magenta arrow loops from station 10 back up to station 7, labeled "11-12 · El broker aprende".

Side column on the right with three small legend cards: "Un solo proceso", "Una inferencia a la vez", "Elige por segundos, no por fama".

Variant without text: same composition, all labels removed, keep numbers only.
```

**Textos exactos:** "01 · de 12" · "La vida de una petición en AI_Broker" · "De la llamada HTTP a la respuesta, y lo que el broker aprende por el camino" · Arranque · Entrada: POST /api/v1/tasks · Cola en SQLite · Despachador: una a la vez · Preparación · ¿Qué estrategia? · ¿Qué modelo? · La llamada al modelo · Agente o debate · Desenlace · 11-12 · El broker aprende · Un solo proceso · Una inferencia a la vez · Elige por segundos, no por fama.

---

## Capítulo 02 · El arranque

**Qué pasa en el código.**
- `scripts/run_broker.py` carga `broker_config.yaml` **una sola vez**, genera un **token de administración nuevo en cada arranque** (salvo que venga fijado desde fuera), lo imprime en consola y lanza Uvicorn con la app ya construida. Así corre en un único proceso, porque el estado vive en SQLite.
- `create_app` (`app/main.py:~115`) monta las piezas en este orden: base de datos → repositorio de tareas → planificador de recursos → **proveedor enrutado** (con acceso diferido a las estadísticas, la cuarentena y las huellas de modelo) → ingesta de ficheros → sandbox → registro MCP → **coordinador**, que las une a todas.
- El `lifespan` (`app/main.py:~163`), al arrancar:
  - avisa de configuraciones incoherentes (plazos que se pisan, proveedores cloud sin precio);
  - arranca LM Studio si hace falta;
  - devuelve a la cola las tareas que quedaron a medias (`recover_interrupted_tasks`, hasta 3 intentos);
  - poda eventos, artefactos y ficheros viejos;
  - lanza los bucles de fondo: despachador de inferencia, despachador de ingesta, descarga de modelos inactivos y precarga de servidores MCP.

**Qué debe transmitir.** El arranque monta las piezas en orden, recupera lo interrumpido y deja cuatro motores girando en segundo plano.

```
Chapter badge: "02 · de 12". Title: "El arranque". Subtitle: "scripts/run_broker.py → create_app → lifespan".

Layout in three horizontal bands, top to bottom:

Band 1 (blue), label "1 · Configuración y llave": a YAML document icon "broker_config.yaml" with an arrow to a key icon "Token admin nuevo en cada arranque".

Band 2, label "2 · Montaje de piezas (create_app)": a row of 8 interlocking blocks assembled left to right, each labeled: "SQLite", "Repositorio", "Planificador de recursos", "Proveedor enrutado" (green), "Ingesta", "Sandbox", "MCP", and a larger final orange block "Coordinador" with thin lines connecting it to all previous blocks.

Band 3, label "3 · Al encender (lifespan)": left side a checklist card with 4 items: "Avisos de configuración", "Arrancar LM Studio", "Recuperar tareas interrumpidas", "Podar lo viejo". Right side four spinning gear icons, each with a label: "Despachador de inferencia" (violet), "Despachador de ingesta" (violet), "Descarga por inactividad" (green), "Precarga MCP" (green).

Footer note in a small card: "Un único proceso: el estado vive en SQLite".

Variant without text: keep bands, blocks and gears; remove words.
```

**Textos exactos:** "02 · de 12" · "El arranque" · "scripts/run_broker.py → create_app → lifespan" · 1 · Configuración y llave · broker_config.yaml · Token admin nuevo en cada arranque · 2 · Montaje de piezas (create_app) · SQLite · Repositorio · Planificador de recursos · Proveedor enrutado · Ingesta · Sandbox · MCP · Coordinador · 3 · Al encender (lifespan) · Avisos de configuración · Arrancar LM Studio · Recuperar tareas interrumpidas · Podar lo viejo · Despachador de inferencia · Despachador de ingesta · Descarga por inactividad · Precarga MCP · Un único proceso: el estado vive en SQLite.

---

## Capítulo 03 · La puerta de entrada

**Qué pasa en el código.** `POST /api/v1/tasks` (`app/main.py:450`) no ejecuta nada. Solo decide si la tarea **puede** entrar y la encola. La regla es fallar pronto: todo lo que depende de la configuración del broker se rechaza aquí, no a mitad de la ejecución.
1. Comprueba el token de administración.
2. Si la petición no trae plazo, hereda el del operador (2.400 s) y queda guardado con la tarea.
3. `run_code` sin sandbox activo → **409 SANDBOX_DISABLED**.
4. Servidores MCP: MCP desactivado → **409 MCP_DISABLED**; servidor desconocido → **409 MCP_SERVER_UNKNOWN**; datos `confidential`/`local_only` hacia un servidor que saca datos fuera → **409 MCP_SERVER_NOT_LOCAL**.
5. Adjuntos: tienen que estar ya convertidos (`ready`). Una hoja de cálculo sin `run_code` → **409 TABULAR_ATTACHMENT_REQUIRES_SANDBOX**.
6. `repository.create_task` (`app/repository.py:102`): la **clave de idempotencia** devuelve la misma tarea si se repite (200) y da error si llega con otro contenido (**409 IDEMPOTENCY_CONFLICT**). Con la cola llena (1.000) → **429 QUEUE_FULL**.
7. Si todo va bien → **202 Accepted**, con la URL para consultar el estado y la de cancelar.

**Qué debe transmitir.** Un embudo de controles: casi nada se ejecuta en la puerta, pero todo lo imposible se rechaza ahí.

```
Chapter badge: "03 · de 12". Title: "La puerta: POST /api/v1/tasks". Subtitle: "Aquí no se ejecuta nada: se decide si la tarea puede entrar".

Main visual: a vertical funnel made of 6 stacked blue checkpoint gates. An envelope labeled "Petición" enters at the top. Each gate is a wide card with a check icon on the left and, on the right, a small red side-exit arrow leading to a red tag with an HTTP code:
Gate 1 "¿Token de administración?" → red tag "403"
Gate 2 "¿Trae plazo? Si no, hereda 2.400 s" → no exit (just a small clock icon)
Gate 3 "¿Pide run_code? ¿Hay sandbox?" → red tag "409 SANDBOX_DISABLED"
Gate 4 "¿Servidores MCP válidos y dentro de la frontera de datos?" → red tag "409 MCP_…"
Gate 5 "¿Adjuntos listos?" → red tag "409 / 404"
Gate 6 "¿Idempotencia y hueco en la cola?" → two red tags "409 IDEMPOTENCY_CONFLICT" and "429 QUEUE_FULL"
At the bottom the envelope drops into a violet queue box with a large green stamp "202 Accepted" and two small link chips "status_url" and "cancel_url".

Right margin callout card: "Fallar pronto: lo imposible se rechaza al crear, no a mitad de ejecución".

Variant without text: funnel, gates, red exits and the final stamp, no words.
```

**Textos exactos:** "03 · de 12" · "La puerta: POST /api/v1/tasks" · "Aquí no se ejecuta nada: se decide si la tarea puede entrar" · Petición · ¿Token de administración? · 403 · ¿Trae plazo? Si no, hereda 2.400 s · ¿Pide run_code? ¿Hay sandbox? · 409 SANDBOX_DISABLED · ¿Servidores MCP válidos y dentro de la frontera de datos? · 409 MCP_… · ¿Adjuntos listos? · 409 / 404 · ¿Idempotencia y hueco en la cola? · 409 IDEMPOTENCY_CONFLICT · 429 QUEUE_FULL · 202 Accepted · status_url · cancel_url · Fallar pronto: lo imposible se rechaza al crear, no a mitad de ejecución.

---

## Capítulo 04 · La cola y el despachador

**Qué pasa en el código.**
- `dispatcher_loop` (`app/dispatcher.py`) da una vuelta cada 0,1 s. Ningún fallo de una vuelta puede matar el bucle: si muriera, la cola crecería sin que nadie la atendiera, con el servidor aparentemente sano.
- `claim_next_queued_task_id` (`app/repository.py:204`) reclama tareas dentro de una transacción SQLite y **solo si no hay otra inferencia activa**. Es el invariante del broker: una inferencia a la vez.
- El orden base es posición en la cola → prioridad → antigüedad, con dos excepciones por la memoria:
  1. **Se salta a quien espera turno.** Una tarea aplazada por falta de memoria tiene `not_before` en el futuro, y mientras tanto pasa la siguiente que sí quepa.
  2. **Salvo que tenga reserva.** Tras ceder el turno 5 veces, la aplazada reserva el suyo y nadie la adelanta. Si no, una petición grande no correría nunca mientras llegaran pequeñas.
- La **ingesta** (convertir PDF, audio o imágenes a Markdown) tiene su propio carril y su propio bucle, con hasta 2 conversiones a la vez. Subir un PDF no congela las respuestas.

**Qué debe transmitir.** Una sola vía para la inferencia, con adelantamientos controlados por la memoria, y una vía paralela para la ingesta.

```
Chapter badge: "04 · de 12". Title: "La cola y el despachador". Subtitle: "Una inferencia a la vez: es el invariante del broker".

Main visual: a top-down road diagram.
Upper road (violet), a single lane labeled "Carril de inferencia · 1 a la vez". A queue of 5 task cards waiting in line, each with a priority number. At the front, a gate with a traffic light labeled "Despachador (cada 0,1 s)". Past the gate, ONE task card inside an orange box labeled "En ejecución".
One card in the queue is lifted slightly to the side with a small hourglass and the tag "Aplazada: no cabe en memoria", and the next card overtakes it with a curved arrow labeled "Pasa la que sí cabe".
A second small scene next to it: the lifted card now wears a lock/reservation badge "Reserva tras 5 cesiones", and the other cards wait behind it.

Lower road (violet, lighter), two parallel lanes labeled "Carril de ingesta · hasta 2 a la vez", with document icons (PDF, audio waveform, image) turning into Markdown file icons "→ .md".

Left side legend card titled "Orden de la cola": three lines "1. Posición", "2. Prioridad", "3. Antigüedad".
Bottom callout: "Si un ciclo falla, el bucle sigue: sin despachador la cola crecería en silencio".

Variant without text: roads, cars-as-cards, gate and traffic light, no words.
```

**Textos exactos:** "04 · de 12" · "La cola y el despachador" · "Una inferencia a la vez: es el invariante del broker" · Carril de inferencia · 1 a la vez · Despachador (cada 0,1 s) · En ejecución · Aplazada: no cabe en memoria · Pasa la que sí cabe · Reserva tras 5 cesiones · Carril de ingesta · hasta 2 a la vez · → .md · Orden de la cola · 1. Posición · 2. Prioridad · 3. Antigüedad · Si un ciclo falla, el bucle sigue: sin despachador la cola crecería en silencio.

---

## Capítulo 05 · Preparación antes de pensar

**Qué pasa en el código.** `ConsensusCoordinator.process_task` (`app/coordinator.py:376`) prepara la tarea antes de tocar ningún modelo:
1. **Echa a los sondeos en sombra** (`shadow_probe.yield_to_real_work`). Son mediciones opcionales y su modelo puede estar ocupando la memoria que ahora hace falta.
2. **Dependencias** (`_await_dependencies`, línea 292): si la tarea declara `depends_on` y aún no se cumplen, vuelve a la cola sin haber gastado nada.
3. **Adjuntos**: si hay imágenes, primero comprueba si algún modelo con visión puede mirarlas. Según eso, la imagen viaja como imagen o como el texto que se le reconozca. Después **expande el prompt** con el Markdown de cada adjunto.
4. **Plazo efectivo** = el menor entre el de la petición y el techo del broker (3.000 s). Toda la ejecución corre dentro de ese reloj (`asyncio.wait_for`).

**Qué debe transmitir.** Cuatro comprobaciones baratas antes de gastar un solo token.

```
Chapter badge: "05 · de 12". Title: "Preparación: antes de gastar un solo token". Subtitle: "ConsensusCoordinator.process_task".

Main visual: a horizontal assembly line with 4 orange stations, a task card moving left to right:
Station 1 icon: a ghost fading away. Label "Fuera los sondeos en sombra". Caption "Liberan la memoria".
Station 2 icon: a chain link. Label "¿Dependencias cumplidas?". Small side arrow back to a violet queue icon: "Si no, vuelve a la cola sin gastar".
Station 3 icon: paperclip with an eye. Label "Adjuntos". Two small branches: "Imagen con visión" and "Imagen como texto (OCR)". Below: a document expanding into the prompt, "El Markdown entra en el prompt".
Station 4 icon: stopwatch. Label "Plazo efectivo". Formula card in large type: "mín(plazo de la petición, 3.000 s)".
At the end, the task card enters a big orange door labeled "Ejecución".

Bottom callout: "Cuatro comprobaciones baratas antes de tocar un modelo".

Variant without text: the assembly line with the four icons, no words.
```

**Textos exactos:** "05 · de 12" · "Preparación: antes de gastar un solo token" · "ConsensusCoordinator.process_task" · Fuera los sondeos en sombra · Liberan la memoria · ¿Dependencias cumplidas? · Si no, vuelve a la cola sin gastar · Adjuntos · Imagen con visión · Imagen como texto (OCR) · El Markdown entra en el prompt · Plazo efectivo · mín(plazo de la petición, 3.000 s) · Ejecución · Cuatro comprobaciones baratas antes de tocar un modelo.

---

## Capítulo 06 · ¿Cómo resolverla? El meta-router

**Qué pasa en el código.**
- Con `strategy: auto`, `_resolve_auto_strategy` (`app/coordinator.py:694`) llama a `classify_request` (`app/strategy_router.py:190`). Es una clasificación **técnica y sin coste**: expresiones regulares sobre el prompt, sin llamar a ningún modelo.
- Precedencia de `strategy_for_signals` (línea 151):
  1. **agent** si hacen falta datos actuales, cálculo exacto o leer una URL.
  2. **mixture_of_agents** si es deliberativa (comparar, analizar, evaluar), el prompt es largo o los datos son sensibles, y hay presupuesto.
  3. **single** en el resto de casos.
- **Aprendizaje:** los casos anteriores con las mismas señales (el "bucket") pueden cambiar la decisión, por ejemplo si en ese tipo de petición el `single` suele acabar escalando.
- **Escalado por confianza:** si sale `single`, un juez puntúa la respuesta. Por debajo del umbral, la tarea escala a `mixture_of_agents` y lo ya gastado se descuenta del presupuesto.
- La decisión queda como evento `strategy.routed`, con sus señales y motivos.

**Qué debe transmitir.** Un cruce de caminos con tres salidas, decidido por señales visibles, corregido por la experiencia y con una vía de escape si la respuesta sale floja.

```
Chapter badge: "06 · de 12". Title: "¿Cómo resolverla? El meta-router". Subtitle: "strategy: auto → una estrategia concreta, sin llamar a ningún modelo".

Main visual: a large orange crossroads signpost in the center. Above it, a "Señales" card listing 6 signal chips with icons: "Datos actuales", "Cálculo exacto", "URL a leer", "Deliberativa", "Prompt largo", "Datos sensibles".
Three roads leave the signpost, ordered by priority with big numbers:
Road 1 → card "agent" with a robot-with-wrench icon, caption "Necesita datos que ningún modelo tiene".
Road 2 → card "mixture_of_agents" with three speech bubbles, caption "Ambigua o delicada, y hay presupuesto".
Road 3 → card "single" with one chip icon, caption "Petición directa".
From the "single" card, a dashed upward arrow to "mixture_of_agents" labeled "Escalado: el juez puntúa bajo".
A magenta notebook icon attached to the signpost labeled "Casos anteriores del mismo tipo pueden cambiar la decisión".

Bottom callout: "Cada decisión se guarda con sus señales y motivos".

Variant without text: signpost, three roads, icons and dashed escalation arrow, no words.
```

**Textos exactos:** "06 · de 12" · "¿Cómo resolverla? El meta-router" · "strategy: auto → una estrategia concreta, sin llamar a ningún modelo" · Señales · Datos actuales · Cálculo exacto · URL a leer · Deliberativa · Prompt largo · Datos sensibles · agent · Necesita datos que ningún modelo tiene · mixture_of_agents · Ambigua o delicada, y hay presupuesto · single · Petición directa · Escalado: el juez puntúa bajo · Casos anteriores del mismo tipo pueden cambiar la decisión · Cada decisión se guarda con sus señales y motivos.

---

## Capítulo 07 · ¿Con qué modelo?

**Qué pasa en el código.** `RoutedModelProvider.select` (`app/providers/routing.py:839`) filtra el catálogo por capas y ordena lo que queda:
1. **Elegible** (`eligible_catalog`, línea 777):
   - **Frontera de datos:** `confidential` y `local_only` solo pueden ir a modelos locales. Es *fail-closed*: lo desconocido cuenta como externo.
   - **Capacidades exigidas:** visión si hay imágenes, generación de imagen si se pide.
   - **Contexto:** el prompt tiene que caber en la ventana del modelo.
2. **Afinidad por tipo de tarea:** `classify_task_type` distingue código, contexto largo y prosa.
3. **Fuera los que están en cuarentena.**
4. **Ranking por tiempo esperado** (`_rank_candidates`, línea 545; `estimate_seconds` en `app/model_timing.py:88`):
   - latencia medida, en caliente o en frío según si el modelo ya está cargado,
   - escalada al tamaño real de esta petición,
   - y **dividida por la tasa de éxito**, porque un modelo que falla obliga a reintentar.
   - Sin evidencia suficiente no se inventa un tiempo: el modelo va detrás de los medidos. La exploración (15 %) y el sondeo en sombra le dan oportunidades.
5. Un `target_model` o `preferred_model` de la petición **manda sobre todo lo anterior**.

**Qué debe transmitir.** Un embudo de filtros y, al final, un podio ordenado por segundos esperados.

```
Chapter badge: "07 · de 12". Title: "¿Con qué modelo? Elegir por segundos". Subtitle: "RoutedModelProvider.select".

Main visual, top half: a wide green funnel with 4 horizontal filter layers; many small model chips enter at the top (some labeled "local", some "cloud"), fewer pass each layer:
Layer 1 "Frontera de datos" – shield icon, note "confidencial → solo local".
Layer 2 "Capacidades exigidas" – eye and image icons, note "visión, imagen".
Layer 3 "¿Cabe en su contexto?" – ruler icon.
Layer 4 "Cuarentena fuera" – red barrier icon.

Bottom half: a podium/leaderboard with 4 rows, each a model chip with a stopwatch and seconds: "12 s", "19 s", "31 s", and a last grey row "sin medir" with a question mark, placed behind the measured ones.
Next to the podium, a large formula card: "Tiempo esperado = latencia (caliente o fría, a este tamaño) ÷ tasa de éxito".
A small dice icon with "15 % de exploración".
A gold override arrow bypassing the whole funnel straight to the top of the podium labeled "target_model / preferred_model manda".

Bottom callout: "Un modelo que falla obliga a reintentar: eso también es espera".

Variant without text: funnel with filter layers, podium with stopwatches, override arrow, no words.
```

**Textos exactos:** "07 · de 12" · "¿Con qué modelo? Elegir por segundos" · "RoutedModelProvider.select" · local · cloud · Frontera de datos · confidencial → solo local · Capacidades exigidas · visión, imagen · ¿Cabe en su contexto? · Cuarentena fuera · 12 s · 19 s · 31 s · sin medir · Tiempo esperado = latencia (caliente o fría, a este tamaño) ÷ tasa de éxito · 15 % de exploración · target_model / preferred_model manda · Un modelo que falla obliga a reintentar: eso también es espera.

---

## Capítulo 08 · La llamada al modelo

**Qué pasa en el código.** La llamada pasa por varias capas, y la última comprueba la respuesta antes de darla por buena:
1. **Checkpoint previo** (`repository.start_invocation`, `app/repository.py:602`): cada intento tiene su fila en `model_invocations`. Si el proceso muere con la llamada en el aire, la fila queda en `started` y la recuperación la trata como ambigua.
2. **Compresión del prompt** (`app/prompt_compressor.py`): quita cortesías y relleno en español y protege byte a byte el código, las URLs y los correos. Se guarda el original; solo viaja comprimido.
3. **Adaptador de proveedor:**
   - `OllamaProvider`, con gestión del ciclo de vida y admisión por memoria unificada;
   - `OpenAICompatibleProvider` (LM Studio, Lemonade, NVIDIA…);
   - `DeepSeekProvider`.
4. **Memoria:** si el modelo no cabe **ahora** (`VRAM_INSUFFICIENT`, `LOCAL_MODEL_SLOTS_BUSY`), la tarea **cede el turno sin fallar** y conserva su sitio en la cola (`_defer_for_memory`, `app/coordinator.py:551`). Si no cabe **nunca** (`VRAM_MODEL_TOO_LARGE`), falla.
5. **Guardias de salida** (`_generate`, `app/providers/routing.py:1170`):
   - primero recorta las fugas de la plantilla de chat;
   - luego rechaza el **eco del prompt**;
   - y después los **bucles degenerativos** (repetir una frase sin fin).
   La salida rechazada se guarda para poder auditarla.
6. **Reintentos:** un error transitorio (429 o 5xx) se reintenta tras 0,5 s y tras 1 s.

**Qué debe transmitir.** Una llamada envuelta en capas de seguridad; lo importante es la inspección de la respuesta antes de aceptarla.

```
Chapter badge: "08 · de 12". Title: "La llamada al modelo". Subtitle: "Cada intento se registra, se comprime, se admite y se inspecciona".

Main visual: a vertical pipeline of 5 concentric-feeling steps, request going down and response coming back up:
Step 1 (green) clipboard icon: "Checkpoint: fila en model_invocations".
Step 2 (green) compress icon: "Compresión del prompt", small note "código y URLs intactos".
Step 3 (green) three plug icons side by side labeled "Ollama", "OpenAI-compatible (LM Studio…)", "DeepSeek".
Step 4 (green) memory chip icon: "¿Cabe en memoria?". Two exits: a violet curved arrow back to the queue "No cabe ahora → cede el turno", and a red arrow "No cabe nunca → falla".
Step 5, on the way back up, an inspection station with a magnifying glass over the response, three check rows with icons: "Fuga de plantilla → se recorta", "Eco del prompt → rechazo", "Bucle repetitivo → rechazo". A small archive box: "La salida rechazada se guarda".
On the side, a small retry loop arrow labeled "Reintentos: 0,5 s y 1 s".

Bottom callout: "Una respuesta que llega entera no es necesariamente una respuesta".

Variant without text: pipeline with icons, inspection station, retry loop, no words.
```

**Textos exactos:** "08 · de 12" · "La llamada al modelo" · "Cada intento se registra, se comprime, se admite y se inspecciona" · Checkpoint: fila en model_invocations · Compresión del prompt · código y URLs intactos · Ollama · OpenAI-compatible (LM Studio…) · DeepSeek · ¿Cabe en memoria? · No cabe ahora → cede el turno · No cabe nunca → falla · Fuga de plantilla → se recorta · Eco del prompt → rechazo · Bucle repetitivo → rechazo · La salida rechazada se guarda · Reintentos: 0,5 s y 1 s · Una respuesta que llega entera no es necesariamente una respuesta.

---

## Capítulo 09 · Estrategia agent: el bucle de herramientas

**Qué pasa en el código.** `_process_agent` (`app/coordinator.py:1679`) y `_run_agent_loop` (línea 1331) dan vueltas hasta `max_iterations`:
1. Antes de cada vuelta ajusta la conversación a la ventana del modelo (`_fit_agent_conversation`).
2. El modelo responde. **Si no pide herramientas, esa es la respuesta final.**
3. Si pide herramientas, cada llamada va a uno de tres sitios:
   - **Skills del broker** (`app/skills.py`): `web_search`, `fetch_url`, `calculator`, `current_datetime` y `run_code`. `run_code` se ejecuta en un contenedor Docker **sin red** y con el sistema de ficheros de solo lectura (`app/sandbox.py`).
   - **Servidores MCP** (`mcp__<servidor>__<tool>`), como Laya.
   - **Herramientas del cliente (passthrough):** la conversación se congela, la tarea pasa a esperar herramientas y el cliente devuelve los resultados por `POST /api/v1/tasks/{id}/tool_results`.
4. Un fallo de herramienta vuelve al modelo como **texto de error**, no como excepción, para que pueda probar otra cosa.
5. Cortes:
   - **presupuesto agotado:** se entrega lo último que dijo el modelo, sin pagar otra vuelta;
   - **iteraciones agotadas:** un turno final sin herramientas para que conteste.
6. **Cotejo de citas** (`app/evidence.py`): las URLs que cita la respuesta se comparan con las que el agente abrió de verdad. Es determinista y no gasta ningún modelo.

**Qué debe transmitir.** Un ciclo "pensar → usar herramienta → leer resultado" con tres cajas de herramientas y salidas claras.

```
Chapter badge: "09 · de 12". Title: "Estrategia agent: el bucle de herramientas". Subtitle: "Pensar → usar una herramienta → leer el resultado → repetir".

Main visual: a big circular loop in orange in the center with 3 stations around it: "El modelo piensa" (brain icon), "Pide una herramienta" (wrench icon), "Lee el resultado" (document icon). An exit arrow from "El modelo piensa" labeled "Sin herramientas → respuesta final" leading to a green check card.

Around the loop, three toolboxes connected to "Pide una herramienta":
Toolbox A (orange) "Skills del broker": small chips "web_search", "fetch_url", "calculator", "current_datetime", "run_code". Next to run_code a sealed container icon with a crossed-out network symbol: "Docker sin red".
Toolbox B (green) "Servidores MCP": chip "mcp__laya__…".
Toolbox C (blue) "Herramientas del cliente": a pause icon and a return arrow "La tarea espera · POST tool_results".

Two stop signs at the edge of the loop: "Presupuesto agotado → lo último que dijo" and "Iteraciones agotadas → turno final sin herramientas".
Magenta magnifier card at the bottom right: "Cotejo de citas: ¿abrió de verdad las URLs que cita?".

Bottom callout: "Un error de herramienta es texto para el modelo, no una excepción".

Variant without text: loop, three toolboxes, stop signs and magnifier, no words.
```

**Textos exactos:** "09 · de 12" · "Estrategia agent: el bucle de herramientas" · "Pensar → usar una herramienta → leer el resultado → repetir" · El modelo piensa · Pide una herramienta · Lee el resultado · Sin herramientas → respuesta final · Skills del broker · web_search · fetch_url · calculator · current_datetime · run_code · Docker sin red · Servidores MCP · mcp__laya__… · Herramientas del cliente · La tarea espera · POST tool_results · Presupuesto agotado → lo último que dijo · Iteraciones agotadas → turno final sin herramientas · Cotejo de citas: ¿abrió de verdad las URLs que cita? · Un error de herramienta es texto para el modelo, no una excepción.

---

## Capítulo 10 · Estrategia mixture_of_agents: el debate

**Qué pasa en el código.** `_process_consensus` (`app/coordinator.py:1858`) avanza por etapas, y cada etapa deja un checkpoint en la tabla `stages`:
1. **Planificación de recursos** (`resource_planning`): elige los proponentes y un **árbitro con otro criterio**. `ResourceScheduler.plan` (`app/resource_scheduler.py:66`) los reparte en **olas** según la memoria: todos a la vez si caben, por tandas o uno detrás de otro.
2. **Propuestas** (`proposing`): cada proponente contesta por su cuenta. Hace falta un **quórum de 2** (o todos, si son menos); si no se alcanza, la tarea falla y explica por qué cayó cada uno.
3. **Síntesis** (`synthesizing`): el árbitro combina las propuestas. Si falla, se prueba un **árbitro sustituto** y, en último caso, se entrega **la mejor propuesta**, marcada como degradada (`_synthesize_with_recovery`, línea 2473).
4. **Segunda ronda** si `max_rounds > 1` y la síntesis no salió degradada: los proponentes ven la síntesis y la mejoran. Si la ronda 2 falla, la tarea queda como estaba tras la primera.
5. El coste de todos se suma y se corta si supera `max_cost_usd`.

**Qué debe transmitir.** Varios modelos proponen, uno arbitra, y siempre hay un plan B que devuelve algo útil.

```
Chapter badge: "10 · de 12". Title: "mixture_of_agents: varios proponen, uno arbitra". Subtitle: "Planificar → proponer → sintetizar → (mejorar)".

Main visual in 4 horizontal stages left to right, connected by thick arrows, each stage header in orange:
Stage 1 "Planificar recursos": a memory bar split into waves; three small model chips grouped as "Ola 1" and "Ola 2". Caption "Olas según la memoria".
Stage 2 "Proponer": three model chips each emitting a speech bubble "Propuesta A", "Propuesta B", "Propuesta C". A counter badge "Quórum: 2".
Stage 3 "Sintetizar": a judge/gavel icon labeled "Árbitro (otro criterio)" merging the three bubbles into one document "Síntesis". Below it, a plan-B ladder of two steps: "Árbitro sustituto" then "Mejor propuesta (degradada)".
Stage 4 "Ronda 2 (opcional)": the three chips reading the synthesis and producing an improved document, caption "Si falla, se queda la ronda 1".
A thin red budget bar running under all stages labeled "Coste acumulado ≤ max_cost_usd".

Bottom callout: "Siempre hay un plan B que devuelve algo útil".

Variant without text: four stages with chips, bubbles, gavel, ladder and budget bar, no words.
```

**Textos exactos:** "10 · de 12" · "mixture_of_agents: varios proponen, uno arbitra" · "Planificar → proponer → sintetizar → (mejorar)" · Planificar recursos · Ola 1 · Ola 2 · Olas según la memoria · Proponer · Propuesta A · Propuesta B · Propuesta C · Quórum: 2 · Sintetizar · Árbitro (otro criterio) · Síntesis · Árbitro sustituto · Mejor propuesta (degradada) · Ronda 2 (opcional) · Si falla, se queda la ronda 1 · Coste acumulado ≤ max_cost_usd · Siempre hay un plan B que devuelve algo útil.

---

## Capítulo 11 · El desenlace

**Qué pasa en el código.** El final de `process_task` (`app/coordinator.py:~420-550`) reparte los resultados:
- **Completada:** el resultado se guarda con su uso de tokens, coste y avisos. Los avisos de dependencias se añaden aquí porque quien los detecta corre antes de que exista el resultado.
- **Cancelada:** se comprueba en cada punto seguro. Si la inferencia ya había ocurrido, primero se cierra su checkpoint con el coste real.
- **TASK_TIMEOUT:** el error dice **dónde se quedó** (por ejemplo, cargando un modelo grande o generando) y **qué plazo mandó**: el techo del broker o el de la petición. Así se sabe cuál subir.
- **Sin memoria ahora:** no es un fallo; la tarea vuelve a la cola aplazada (capítulo 04).
- **Otro error de proveedor:** falla y marca la etapa en la que murió.
- **Siempre, pase lo que pase:**
  - se limpia la espera de memoria;
  - se registra el **caso de enrutamiento** para el aprendizaje del meta-router;
  - y **solo si terminó bien**, se programa un **sondeo en sombra**.
- El cliente ve todo esto con `GET /api/v1/tasks/{id}`, sus invocaciones y artefactos, o en el panel `/dashboard`.

**Qué debe transmitir.** Cinco finales posibles, cada uno con su explicación, y tres acciones que ocurren siempre.

```
Chapter badge: "11 · de 12". Title: "El desenlace". Subtitle: "Cómo termina una tarea, y qué deja siempre detrás".

Main visual: a task card at the top splitting into 5 branches, each ending in a large end-state card:
1 green check "Completada" – caption "Resultado, tokens, coste y avisos".
2 grey stop "Cancelada" – caption "Se cierra el coste real de lo ya hecho".
3 red stopwatch "TASK_TIMEOUT" – caption "Dice dónde se quedó y qué plazo mandó".
4 violet hourglass "Aplazada por memoria" – caption "Vuelve a la cola: no es un fallo", with a curved arrow back to a small queue icon.
5 red warning "Error del proveedor" – caption "Marca la etapa donde murió".

Below all branches, a magenta band titled "Siempre, pase lo que pase" with three icons in a row: broom "Limpiar la espera de memoria", notebook "Guardar el caso de enrutamiento", ghost "Sondeo en sombra (solo si salió bien)".

Right side, a small blue screen icon with "GET /api/v1/tasks/{id} · /dashboard".

Bottom callout: "Un fallo que explica su causa se puede arreglar".

Variant without text: five branches with end-state icons and the magenta band, no words.
```

**Textos exactos:** "11 · de 12" · "El desenlace" · "Cómo termina una tarea, y qué deja siempre detrás" · Completada · Resultado, tokens, coste y avisos · Cancelada · Se cierra el coste real de lo ya hecho · TASK_TIMEOUT · Dice dónde se quedó y qué plazo mandó · Aplazada por memoria · Vuelve a la cola: no es un fallo · Error del proveedor · Marca la etapa donde murió · Siempre, pase lo que pase · Limpiar la espera de memoria · Guardar el caso de enrutamiento · Sondeo en sombra (solo si salió bien) · GET /api/v1/tasks/{id} · /dashboard · Un fallo que explica su causa se puede arreglar.

---

## Capítulo 12 · El broker aprende

**Qué pasa en el código.** Cada invocación deja evidencia, y esa evidencia cambia la siguiente elección del capítulo 07. Es un ciclo cerrado:
- **Métricas por modelo y tipo de tarea** (`app/model_stats.py`): se agregan desde `model_invocations` (últimos 7 días), separadas por código, contexto largo y prosa. Un modelo bueno en prosa no hereda ese historial en código.
- **Tiempo esperado** (`app/model_timing.py`): se calcula con esas métricas (capítulo 07).
- **Cuarentena** (`app/model_quarantine.py`): 2 fallos definitivos seguidos (respuesta inválida, eco del prompt, bucle, modelo incompatible…) apartan al modelo **24 horas**. Caduca sola, así que un modelo reinstalado vuelve sin que nadie lo toque.
- **Sondeo en sombra** (`app/shadow_probe.py`): tras una tarea correcta, se lanza el mismo prompt a **un aspirante sin evidencia** solo para cronometrarlo. Su salida se descarta y nadie espera por ella. Cede el sitio en cuanto llega trabajo real.
- **Huellas de ejecución** (`app/execution_fingerprint.py`, `app/model_fingerprints.py`): registran cuantización, digest, plantilla y runtime. Si cambia algo, queda constancia, porque "este modelo se ha vuelto tonto" suele ser "le cambiaron la plantilla".
- **Casos de enrutamiento:** alimentan al meta-router (capítulo 06).
- **`exclude_from_model_learning`:** una tarea marcada así no enseña nada. Sirve para que las pruebas adversariales no hundan a un modelo en producción.

**Qué debe transmitir.** Un círculo virtuoso: ejecutar → medir → decidir mejor, con frenos para no aprender de lo que no debe.

```
Chapter badge: "12 · de 12". Title: "El broker aprende". Subtitle: "Cada invocación deja evidencia; la evidencia decide la siguiente elección".

Main visual: a large magenta circular cycle with 4 nodes:
Node 1 "Ejecutar" (green chip icon) → Node 2 "Registrar invocación" (database icon, caption "model_invocations") → Node 3 "Métricas por modelo y tipo de tarea" (bar chart icon, three small tabs "código", "contexto largo", "prosa") → Node 4 "Tiempo esperado" (stopwatch) → back to Node 1 with arrow labeled "Mejor elección".

Four satellite cards around the cycle, each connected by a thin line:
Red barrier card "Cuarentena: 2 fallos seguidos → 24 h fuera".
Ghost card "Sondeo en sombra: mide un aspirante sin que nadie espere".
Fingerprint card "Huella: detecta cambios de plantilla, cuantización o runtime".
Signpost card "Casos de enrutamiento → meta-router".

A grey shield near the entry of the cycle: "exclude_from_model_learning: esta tarea no enseña nada".

Bottom callout: "Responder y aprender, separados: nadie espera por una medición".

Variant without text: cycle with four nodes, satellites and shield, no words.
```

**Textos exactos:** "12 · de 12" · "El broker aprende" · "Cada invocación deja evidencia; la evidencia decide la siguiente elección" · Ejecutar · Registrar invocación · model_invocations · Métricas por modelo y tipo de tarea · código · contexto largo · prosa · Tiempo esperado · Mejor elección · Cuarentena: 2 fallos seguidos → 24 h fuera · Sondeo en sombra: mide un aspirante sin que nadie espere · Huella: detecta cambios de plantilla, cuantización o runtime · Casos de enrutamiento → meta-router · exclude_from_model_learning: esta tarea no enseña nada · Responder y aprender, separados: nadie espera por una medición.

---

## Índice de ficheros citados

| Capítulo | Ficheros |
|---|---|
| 02 | `scripts/run_broker.py`, `app/main.py` (`create_app`, `lifespan`), `app/startup.py` |
| 03 | `app/main.py:450`, `app/repository.py:102`, `app/schemas.py` (`TaskCreateRequest`) |
| 04 | `app/dispatcher.py`, `app/repository.py:204` y `:488` |
| 05 | `app/coordinator.py:376`, `:292`; `app/ingestion/service.py` |
| 06 | `app/coordinator.py:694`, `:1269`; `app/strategy_router.py:151`, `:190` |
| 07 | `app/providers/routing.py:545`, `:777`, `:839`; `app/model_timing.py:88`; `app/task_classifier.py:246` |
| 08 | `app/repository.py:602`; `app/prompt_compressor.py`; `app/providers/ollama.py`, `openai_compatible.py`, `deepseek.py`; `app/providers/routing.py:1170`; `app/coordinator.py:551` |
| 09 | `app/coordinator.py:1331`, `:1679`; `app/skills.py`; `app/sandbox.py`; `app/mcp.py`; `app/evidence.py` |
| 10 | `app/coordinator.py:1858`, `:2473`; `app/resource_scheduler.py:66` |
| 11 | `app/coordinator.py:376-550`; `app/main.py:543` |
| 12 | `app/model_stats.py`, `app/model_timing.py`, `app/model_quarantine.py`, `app/shadow_probe.py`, `app/execution_fingerprint.py`, `app/model_fingerprints.py` |
