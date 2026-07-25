# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Operador único (el autor).** Ejecuta el broker en su propia máquina Windows 11 y mantiene el panel **abierto en un segundo monitor** mientras trabaja en otra cosa. No consulta el panel: lo tiene de fondo y lo mira de reojo. Su pregunta recurrente no es "¿qué hace este sistema?" sino "¿ha cambiado algo que deba atender?". Conoce el sistema por completo — jerga técnica, VRAM, quórum, proveedores y estrategias son vocabulario propio, no barreras.

**Tribunal de TFM (Ingeniería / IA).** Segundo público real y con fecha. Evalúa el proyecto en dos contextos confirmados:

1. **Capturas estáticas dentro de la memoria escrita** — cada pantalla debe sostener su historia congelada, sin interacción, sin movimiento y sin tooltips que expliquen lo que la imagen no muestra.
2. **Ejecutando el proyecto por su cuenta** — en una máquina ajena, sin los modelos del autor, sin claves de API y sin historial en SQLite. El primer arranque en frío es una superficie evaluada, no un detalle.

Juzgan arquitectura, rigor técnico y decisiones de ingeniería. El panel puntúa en la medida en que hace **visible por dentro** lo que el sistema hace: trazas, evidencia acumulada, decisiones del meta-router y sus motivos.

*Decisión abierta:* si además habrá demo en vivo proyectada el día de la defensa. No confirmado.

## Product Purpose

AI Broker es una central privada de inferencia: un único punto de entrada que recibe peticiones, documentos, imágenes, audio y vídeo, elige el mejor modelo o modelos disponibles (locales o en la nube) y devuelve la respuesta, con trazabilidad completa de cada decisión.

El panel web local (`http://127.0.0.1:8765/dashboard`) es la cara operativa del servicio: encolar y probar inferencias, vigilar la cola y los recursos, inspeccionar qué modelo se eligió y por qué, y editar la configuración en caliente sin tocar el YAML a mano.

**Éxito** tiene dos caras simultáneas y no negociables entre sí:

- *Operativo:* el autor detecta desde el rabillo del ojo que algo requiere su atención, y cuando entra a mirar encuentra la causa sin navegar a ciegas.
- *Académico:* un tribunal de ingeniería que nunca ha visto el sistema entiende, sólo mirando el panel, que las decisiones del broker están fundamentadas en evidencia y no en heurísticas improvisadas.

## Positioning

Un gateway de inferencia multi-LLM que **no depende de ninguna nube para funcionar** y que trata cada decisión de enrutamiento como una afirmación que debe poder defenderse con datos propios.

Lo que un producto vecino no podría copiar honestamente:

- **Frontera de datos aplicada por contrato, no por convención.** `data_classification: local_only` desactiva los proveedores cloud a nivel de validación Pydantic; `cloud_allowed: false` es el valor por defecto. No es una promesa de política de privacidad: es una restricción que hace fallar la petición.
- **Selección de modelo por evidencia medida en tu propia máquina.** Tasa de éxito con suavizado de Laplace, latencia y coste normalizados sobre `model_invocations` reales, segmentados por tipo de tarea. Un modelo sin historial puntúa neutro; el arranque en frío no castiga. La exploración es dirigida (rematar al que está a medias antes de estrenar a otro), no un sorteo uniforme.
- **Capacidades sondeadas, no declaradas.** Jerarquía explícita de evidencia: sondeo real contra el endpoint > capacidades del runtime > catálogo models.dev > heurística por nombre. El catálogo rellena huecos; nunca pisa un dato verificado.
- **Deliberación multi-modelo con anti-inyección sistemática.** Candidatos, documentos adjuntos y resultados de herramientas viajan en sandboxes XML con delimitadores neutralizados: un candidato no puede inyectar instrucciones al árbitro, ni un PDF cerrar su propio tag.
- **Ejecución de código generado por IA con fronteras no configurables.** `--network none`, sin volúmenes del host, `--read-only`, usuario `nobody`, `--cap-drop ALL`. Doblemente opt-in.

## Operating Context

- **Máquina:** Windows 11 Pro, GPU única compartida por todos los modelos locales. El `ResourceScheduler` arbitra la VRAM y decide `parallel` / `waves` / `sequential`.
- **Acceso:** loopback `127.0.0.1:8765`. Arranque *fail-closed*: el broker se niega a escuchar fuera de loopback sin token admin.
- **Restricción de entorno conocida y persistente:** el navegador Chrome del autor **no alcanza ningún puerto loopback** (interceptor o proxy interno). Consecuencia registrada en `design-qa.md`: el QA visual de la fase 5.2 quedó en `final result: blocked` porque no se pudo abrir el panel para comparar referencia contra implementación. Cualquier flujo de trabajo que dependa de conducir el panel desde el navegador está bloqueado hasta que se resuelva.
- **Arranque cotidiano:** `arrancar_ai_broker.bat` (doble clic), `parar_ai_broker.bat`, `estado_ai_broker.bat`.
- **Servicios que orbitan:** Ollama y LM Studio en local; DeepSeek y NVIDIA como cloud opcional con clave en keyring; Docker Desktop sobre WSL2 para el sandbox; `ffmpeg` para vídeo.
- **Ritmo de uso:** panel de fondo en segundo monitor durante la jornada, con incursiones puntuales al probador, a comparación o a configuración.

## Capabilities and Constraints

**Superficies del panel (8 páginas):** Resumen · Tareas · Probador · Comparación · Modelos · Ficheros · Enrutamiento · Configuración.

**Capacidades confirmadas:**

- Cuatro estrategias: `single`, `mixture_of_agents` (presets `fast`/`slow`), `agent` (tool-calling con guardarraíles) y `auto` (meta-router con clasificador determinista, escalado por confianza y aprendizaje adaptativo persistido en `routing_cases`).
- Ingesta de adjuntos a Markdown: PDF (con OCR por página), Office, eBook, imágenes, audio y vídeo. Dedupe por SHA-256, tokens estimados por fichero.
- Sandbox Docker desechable para código generado por modelos.
- Compresión de prompts determinista por reglas, con vista previa fiel y evento `prompt.compressed` persistido.
- Cola durable con reordenación, cancelación idempotente y recuperación al arranque.
- Edición del YAML en caliente desde el panel, con detección de edición concurrente por huella SHA-256 y revisión de cambios antes de aplicar.

**Restricciones técnicas vinculantes:**

- **Stack sin build step:** Jinja2 + fragmentos hipermedia HTMX. No hay bundler, no hay framework de componentes, no hay paso de compilación de assets.
- **Cero recursos externos:** CSS y JS 100% locales, sin CDN, sin fuentes remotas. Aplicado por CSP, no por disciplina. Es una restricción de privacidad, no de estilo.
- **Auto-refresco por bloque** con backoff exponencial, pausa con pestaña oculta y banner de conexión persistente en lugar de tormenta de toasts.
- **Sin métricas de streaming:** no se muestran tokens, tokens/s ni porcentaje de generación en directo. Los proveedores actuales usan `stream=false` y devuelven esas cifras al terminar. Durante una llamada individual la barra es indeterminada, y así debe seguir.
- **Un solo workflow activo global** (`max_active_workflows: 1`). La UI muestra `N pendientes · 1 slot LLM`; nunca "trabajadores".
- **Contrato Pydantic estricto** (v2.5, `extra="forbid"`). El panel es un cliente del broker: consulta estado, encola inferencias ya preparadas, reordena y cancela. No llama a proveedores directamente.

**Terminología del producto** (no traducir ni suavizar en la UI): tarea, cola, slot LLM, proponente, árbitro, quórum, estrategia, preset, deployment, invocación, evento, artefacto, skill, sandbox, adjunto, ingesta, VRAM reservable, oleada, huella de señales, caso de enrutamiento.

**Decisión abierta:** el idioma del panel es español y no se declaró compromiso vinculante de mantenerlo así. La i18n queda posible, no planificada.

## Brand Commitments

- **Nombre:** AI Broker.
- **Tema oscuro obligatorio.** Declarado en `Agent_AI_Broker.md` y confirmado como restricción dura, no como preferencia revisable. No existe modo claro y no debe proponerse uno.
- **Cero métricas simuladas.** Todo dato visible procede de SQLite, del health check o de un snapshot real de recursos con su timestamp. Sin placeholders, sin datos de ejemplo, sin barras de progreso inventadas, sin porcentajes estimados presentados como medidos. Cuando falta un dato se dice `N/D` con su motivo; no se rellena.
- **Toda cifra lleva su procedencia.** Ventana temporal explícita, denominador visible, hora de comprobación. Una latencia p95 sin periodo declarado es un dato inválido en este producto.

## Evidence on Hand

**Datos reales disponibles:** `tasks` y cola durable · `model_invocations` (latencia, coste, éxito, tipo de tarea) · `routing_cases` (aprendizaje del meta-router por huella de señales) · `features` (capacidades sondeadas con timestamp) · eventos de *event sourcing* de cada mutación · snapshot de VRAM vía Ollama `/api/ps` · health check de SQLite, dispatcher y proveedores · catálogo models.dev cacheado en disco (contexto real, precios por 1M, corte de conocimiento).

**Corpus de regresión:** `tests/fixtures/ingestion/` (corpus dorado de ingesta).

**Documentación normativa existente:** `README.md`, `Agent_AI_Broker.md` (contrato de integración), `docs/Phase_5_Dashboard.md` (normativo para las pantallas), `docs/Phase_7_File_Ingestion.md`, `docs/Phase_8_Sandbox.md`, `docs/Prompt_Compression.md`, `Deployment_Guide.md`.

**Ausencias que ningún trabajo futuro debe fabricar:**

- No hay usuarios reales más allá del autor. Sin testimonios, sin casos de estudio, sin logos de clientes, sin cifras de adopción.
- No hay benchmarks publicados ni comparativas contra productos de terceros.
- No hay modelo de negocio, precios ni planes: el proyecto es MIT.
- No hay identidad visual encargada: sin logotipo profesional, sin fotografía de producto, sin ilustración a medida.
- El QA visual del panel nunca se ha completado (ver Operating Context). No debe afirmarse que las pantallas están validadas visualmente.

## Product Principles

1. **La evidencia es el producto.** El broker existe porque decide con datos medidos en tu máquina. Todo lo que el panel muestra debe poder rastrearse hasta su fuente, y la fuente debe ser visible sin pedirla.
2. **Legible de reojo antes que denso.** Vive en un segundo monitor. Un cambio que exige atención tiene que ganarse la mirada periférica; lo que no ha cambiado no debe reclamarla.
3. **Ausencia declarada, nunca rellenada.** Un hueco de datos se nombra con su motivo. Inventar un valor plausible destruye la única propiedad que este producto vende.
4. **La frontera de datos se ve.** Local vs. cloud, aislado vs. con red, autenticado vs. abierto: son las decisiones de mayor consecuencia del sistema y no pueden ser letra pequeña.
5. **Sin ceremonia para quien ya sabe.** El operador conoce el sistema entero. Nada de tutoriales, confirmaciones redundantes ni explicaciones de vocabulario propio — con una excepción: el primer arranque en una máquina ajena y vacía, que es superficie evaluada.

## Accessibility & Inclusion

Requisitos personales del operador principal, registrados como restricciones de producto de primera clase:

- **El texto pequeño cuesta.** El panel necesita un cuerpo de texto mayor y una densidad menor de la que un dashboard técnico suele asumir por defecto. La densidad no es una virtud aquí: la legibilidad gana cuando compiten.
- **Fatiga visual en sesiones largas.** Muchas horas frente al monitor. Deben evitarse las superficies puras (`#000` / `#fff`) y el contraste extremo sostenido; la paleta oscura debe ser templada, con el contraste reservado para lo que de verdad debe destacar.

Estas dos necesidades convergen con el segundo contexto de evaluación (capturas dentro de la memoria escrita y posible proyección en sala): lo que hace el panel más cómodo para su operador también lo hace más legible para el tribunal.

Sin compromiso formal de auditoría contra WCAG 2.1 AA. Higiene profesional esperada de todos modos: recorrido completo por teclado, foco visible y respeto a `prefers-reduced-motion`.
