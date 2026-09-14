# AI Broker — Guion de Formación y Diapositivas: Módulos 3 y 4

Este documento contiene el guion de producción audiovisual, contenido de diapositivas y pautas de demostración en vivo sobre las pantallas originales de la app para los **Módulos 3 y 4**.

---

# Módulo 3: Ingesta Documental y Procesamiento Multimodal

* **Duración estimada:** ~12 minutos  
* **Objetivo didáctico:** Dominar la pantalla de Ficheros (`/dashboard/files`), la conversión desatendida a Markdown, la gestión de figuras e imágenes con modelos de visión, la transcripción local de audio/vídeo mediante Faster-Whisper, y la inyección segura de adjuntos con prevención de desbordamiento mediante Map-Reduce.

---

### Diapositiva 3.1 — De Documento a Conocimiento: El Pipeline de Ingesta
* **Contenido de la Diapositiva:**
  * **Título:** Ingesta Documental: Transformando Archivos en Markdown Auditable
  * **Matriz de formatos soportados:**
    | Tipo | Formatos | Motor | Comportamiento |
    |---|---|---|---|
    | **PDF (nativo/escaneo)** | `.pdf` | Docling + RapidOCR/EasyOCR | OCR selectivo página a página |
    | **Office / Web / Libros** | `.docx, .xlsx, .pptx, .html, .epub` | MarkItDown | Conversión a Markdown estructurado |
    | **Código y Texto** | `.py, .js, .json, .csv, .txt, .sql` | Passthrough | Envoltorio en bloques de código |
    | **Audio / Vídeo** | `.mp3, .wav, .mp4, .mkv, .mov` | ffmpeg + Faster-Whisper | Extracción de pista y transcripción local |
    | **Imágenes** | `.png, .jpg, .webp` | Nativo Visión | No se convierten: viajan intactas al modelo |
  * **Puntos clave:** Deduplicación por hash SHA-256 (subir dos veces el mismo archivo no repite trabajo) y estimación previa de tokens.
* **Pantalla de la App a mostrar:**
  * [Pantalla de Ficheros](file:///c:/Procesos/AI_Broker/app/templates/files.html) (`/dashboard/files`).
  * Mostrar el formulario de subida y desplegar la sección *"Formatos admitidos"*.
* **Guion Locutado (Voz en off / Presentador):**
  > *"Bienvenidos al Módulo 3. En el trabajo real de ingeniería o empresa, rara vez interactuamos con una IA pasándole únicamente texto plano. Lo normal es tener que analizar un informe financiero en PDF, una hoja de cálculo con miles de filas, una grabación de una reunión o una captura técnica.*  
  > *El AI Broker incorpora una central de ingesta documental de primer nivel. En lugar de enviar un archivo binario crudo y confiar en que el proveedor sepa qué hacer con él, el broker lo normaliza a Markdown estructurado antes de que llegue al LLM. Si el PDF es un escaneo, aplica OCR página a página; si es un vídeo, extrae el audio y lo transcribe con Whisper en tu propio hardware; y si es una imagen, no la sustituye por una descripción empobrecida: la mantiene entera para que la examine un modelo con ojos de verdad."*
* **Acción en la Demo en Pantalla:**
  * Navegar a la sección **Ficheros** en la barra lateral.
  * Hacer clic en el desplegable `<details>` de "Formatos admitidos" mostrando la lista completa de extensiones reconocidas.

---

### Diapositiva 3.2 — Caso de Uso 5: Subida y Conversión de PDFs con OCR y Figuras
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 5 — Ingesta de PDF con Tablas y Figuras
  * **Opciones en el formulario:**
    * `Fichero`: Arrastre de documento PDF.
    * `Imágenes del documento`: Describirlas con LLM de visión vs. Omitirlas (máxima velocidad).
    * `OCR`: Detección automática por página en documentos escaneados.
  * **Estados de la tabla:** `converting` (insignia amarilla en cola) ➔ `ready` (insignia verde con tokens estimados).
* **Pantalla de la App a mostrar:**
  * **Ficheros** (`/dashboard/files`): Proceso de subida de un PDF técnico.
  * [Visor de Markdown](file:///c:/Procesos/AI_Broker/app/dashboard_web.py#L532) (`/dashboard/files/{id}/markdown`): Texto limpio generado.
* **Guion Locutado:**
  > *"Veamos el proceso en acción. Seleccionamos un PDF complejo con tablas y gráficos. En el desplegable 'Imágenes del documento' podemos elegir si queremos que las figuras internas se envíen a un modelo de visión para que redacte una descripción que se insertará en el Markdown final, o si preferimos omitirlas para una conversión ultrarrápida.*  
  > *Pulsamos 'Subir'. La página nos redirige y vemos el archivo en estado 'converting'. Recordad lo que vimos en el Módulo 1: esta conversión corre en el carril paralelo de ingesta, de modo que si hay inferencias en cola, nadie espera.*  
  > *En cuanto termina, pasa a 'ready' y la tabla nos enseña el número de páginas, el motor Docling utilizado y una cifra vital: los tokens estimados. Al hacer clic en 'Ver Markdown', comprobamos cómo las tablas se han transformado en formato Markdown legible y limpio."*
* **Acción en la Demo en Pantalla:**
  1. Subir un PDF de muestra desde el formulario.
  2. Observar la fila en la tabla de ficheros con el refresco automático HTMX.
  3. Al quedar `ready`, hacer clic en **Ver Markdown** para abrir en una pestaña el texto estructurado resultante.

---

### Diapositiva 3.3 — Caso de Uso 6: Transcripción Local de Audio y Vídeo
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 6 — Transcripción Desatendida con Faster-Whisper
  * **Pipeline multimedia:**
    * Archivo de vídeo (.mp4/.mkv) ➔ `ffmpeg` extrae la pista de audio a 16 kHz mono.
    * `faster-whisper` local transcribe el audio sin enviar nada a APIs externas.
    * Metadatos generados: duración exacta formateada (ej. `12 min 34 s`) y texto transcrito.
* **Pantalla de la App a mostrar:**
  * **Ficheros** (`/dashboard/files`) con una grabación de audio/vídeo terminada.
  * **Resumen Operativo** (`/dashboard`) mostrando el monitor de carriles de trabajo.
* **Guion Locutado:**
  > *"¿Qué ocurre con el material audiovisual? Subir grabaciones de reuniones a servicios cloud suele ser problemático por cuestiones de confidencialidad y RGPD.*  
  > *En AI Broker, al subir un archivo de audio o vídeo, el sistema invoca ffmpeg para aislar el sonido y pasa la pista a Faster-Whisper, configurado para ejecutarse en CPU o GPU según el hardware disponible. La tabla nos muestra la duración formateada y genera el Markdown con todo lo hablado.*  
  > *Si echamos un vistazo al panel Resumen mientras esto ocurre, comprobamos la transparencia del sistema: el widget 'Carga de la máquina' refleja la conversión activa sin alterar el slot de inferencia."*
* **Acción en la Demo en Pantalla:**
  * Mostrar una fila de la tabla de ficheros correspondiente a un archivo `.mp4` o `.mp3`, señalando la columna `Duración` y el enlace a su transcripción en Markdown.

---

### Diapositiva 3.4 — Caso de Uso 7: Tratamiento de Imágenes Sueltas y Visión Pura
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 7 — Imágenes Nativas y Modelos Multimodales
  * **Diferencia conceptual crítica:**
    * Un documento con texto se convierte a Markdown.
    * **Una imagen suelta NO se convierte a texto:** Nace directamente en estado `ready` (sin ocupar el carril de conversión).
    * En el despacho al LLM, los bytes viajan íntegros en base64 o URL al adaptador de visión (Ollama / OpenAI).
    * **Fallo explícito:** Si se intenta procesar una imagen con un modelo solo de texto, el Broker falla con `VISION_MODEL_UNAVAILABLE` en vez de responder con una invención.
* **Pantalla de la App a mostrar:**
  * **Ficheros** (`/dashboard/files`): Fila de imagen con el botón *"Ver imagen"*.
  * [Visor de Imagen Original](file:///c:/Procesos/AI_Broker/app/dashboard_web.py#L571) (`/dashboard/files/{id}/original`).
  * **Probador de Prompts**: Selección del adjunto y filtrado automático de modelos compatibles.
* **Guion Locutado:**
  > *"Prestad mucha atención a este principio: una imagen nunca debe reducirse a un párrafo de texto si hay un modelo capaz de mirarla con sus propios ojos. Describir una imagen en texto antes de enviarla pierde de forma irrecuperable todo lo que ese párrafo no mencione.*  
  > *Por eso, al subir un PNG o JPG en la pestaña Ficheros, el archivo queda en estado 'ready' al instante, sin pasar por el carril de ingesta, y nos ofrece el enlace 'Ver imagen' para inspeccionarlo.*  
  > *Cuando vamos al Probador y seleccionamos esta imagen, el catálogo actúa con rigor: filtra los modelos y solo nos permite elegir aquellos cuyo sondeo confirmó que soportan visión. Si no hubiera ninguno disponible, la tarea falla formalmente explicándolo, en lugar de simular que ha visto la imagen leyendo su nombre de archivo."*
* **Acción en la Demo en Pantalla:**
  1. Mostrar una imagen en la tabla de Ficheros y hacer clic en **Ver imagen**.
  2. Ir al Probador, marcar la casilla de la imagen en la lista de adjuntos.
  3. Abrir el selector de modelos y observar cómo solo aparecen los modelos con capacidad `Visión`.

---

### Diapositiva 3.5 — Caso de Uso 8: Prevención de Desbordamiento con Map-Reduce
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 8 — Manejo de Grandes Volúmenes: Map-Reduce
  * **El problema:** Documentos adjuntos que superan los tokens de contexto del modelo.
  * **La solución de AI Broker:**
    * Sin la casilla marcada: Fallo tipado `CONTEXT_LIMIT_EXCEEDED` explicando las cotas (nunca trunca en silencio).
    * Con la casilla `long_context_map_reduce` marcada: El broker trocea el documento en fragmentos coherentes, ejecuta una pasada de extracción (Map) y sintetiza la conclusión final (Reduce).
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts**: Casilla *"Trocear si los adjuntos no caben (map-reduce)"*.
* **Guion Locutado:**
  > *"Uno de los errores más comunes y peligrosos en aplicaciones de IA es el truncado silencioso: el usuario adjunta un informe de 100 páginas, el modelo solo tiene ventana para 20, y el sistema corta el resto sin avisar, respondiendo con datos incompletos.*  
  > *AI Broker prohíbe el truncado silencioso por contrato. Si un documento no cabe, la tarea falla indicando los números exactos. Sin embargo, en el Probador disponemos de la casilla 'Trocear si los adjuntos no caben (map-reduce)'. Al activarla, autorizamos al Broker a procesar el texto por bloques y sintetizar después el resultado, garantizando que todo el contenido se analice sin exceder la memoria del modelo."*
* **Acción en la Demo en Pantalla:**
  * Enfocar en el Probador de Prompts la casilla `Trocear si los adjuntos no caben (map-reduce)` y leer su texto de ayuda contextual.

---

# Módulo 4: Consenso Multi-Modelo (Mixture of Agents)

* **Duración estimada:** ~15 minutos  
* **Objetivo didáctico:** Comprender la arquitectura de deliberación multi-modelo (MoA), los presets `fast` (serial) y `slow` (paralelo/oleadas con arbitraje de VRAM), la prevención de inyecciones de prompt entre modelos, la pantalla de Comparación forense (`/dashboard/comparison`), la resiliencia ante caídas del árbitro y el consenso de doble ronda.

---

### Diapositiva 4.1 — Por Qué Deliberar: La Filosofía de Mixture of Agents
* **Contenido de la Diapositiva:**
  * **Título:** Deliberación Multi-Modelo: Cuando un Solo LLM No Basta
  * **Estructura del pipeline:**
    ```
    Petición Original ──► [Proponente 1: Rol Specialist]  ──┐
                      ──► [Proponente 2: Rol Skeptic]     ──┼─► [Sandboxes XML] ──► [Árbitro / Synthesizer] ──► Respuesta Final
                      ──► [Proponente 3: Rol Analyst]     ──┘
    ```
  * **Roles auditables:** `generalist`, `specialist`, `skeptic`, `analyst`, `reviewer`.
  * **Defensa anti-inyección:** Cada propuesta viaja dentro de delimitadores XML neutralizados (`<original_request>`, `<candidate_1>`, `<candidate_2>`). Un proponente hostil no puede cerrar tags ni inyectar órdenes al árbitro.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts** con la estrategia `mixture_of_agents` desplegada.
* **Guion Locutado:**
  > *"Entramos en una de las capacidades más potentes y avanzadas de AI Broker: el consenso multi-modelo o Mixture of Agents. En tareas críticas —como una auditoría de código, una decisión legal o un análisis estratégico— confiar en la respuesta de un único modelo es arriesgado: puede tener sesgos, alucinaciones o lagunas.*  
  > *AI Broker permite reunir a un comité de especialistas. Varios modelos analizan la misma petición bajo distintos roles: un especialista técnico, un escéptico que busca fallos y un analista que comprueba la coherencia. Después, un modelo árbitro lee todas las propuestas y redacta una síntesis equilibrada.*  
  > *Para evitar que un modelo intente manipular al árbitro, AI Broker aplica un sandbox XML estricto: las respuestas viajan con delimitadores neutralizados para que ninguna propuesta pueda hacerse pasar por una instrucción de sistema."*
* **Acción en la Demo en Pantalla:**
  * En el Probador, seleccionar **Estrategia: Mixture of LLMs**. Mostrar la lista de proponentes manuales del 1 al 5 con sus campos de rol y el selector de árbitro.

---

### Diapositiva 4.2 — Caso de Uso 9: Mixture Fast (Ejecución Serial Segura)
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 9 — Consenso Serial Rápido (`preset: fast`)
  * **Características operativas:**
    * Concurrencia uno: Proponente 1 ➔ Proponente 2 ➔ Árbitro.
    * Garantiza que la GPU nunca intente cargar dos modelos simultáneamente.
    * Quórum mínimo: Al menos 2 proponentes deben responder con éxito; si no, la tarea falla con `CONSENSUS_QUORUM_NOT_REACHED`.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts**: Preset `fast` seleccionado.
  * [Pantalla de Comparación](file:///c:/Procesos/AI_Broker/app/templates/comparison.html) (`/dashboard/comparison`): Línea temporal escalonada.
* **Guion Locutado:**
  > *"Configuremos un mixture en modo 'fast'. Elegimos dos proponentes en Ollama, por ejemplo Qwen-Coder como especialista y Llama-3 como escéptico, y seleccionamos como árbitro un modelo de mayor capacidad.*  
  > *En el preset fast, la ejecución es rigurosamente serial: primero se ejecuta el primer modelo, se libera la memoria necesaria, entra el segundo proponente y finalmente el árbitro sintetiza. Es la opción ideal para máquinas con GPUs discretas de gama media, donde queremos la máxima calidad de debate sin comprometer la estabilidad térmica o la memoria de la tarjeta."*
* **Acción en la Demo en Pantalla:**
  * Encolar una prueba `fast` y abrir la pantalla de **Comparación**, observando cómo la columna izquierda lista la tarea y la derecha despliega la línea temporal.

---

### Diapositiva 4.3 — Caso de Uso 10: Mixture Slow y Planificación de VRAM por Oleadas
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 10 — Paralelismo Real y Oleadas (`preset: slow`)
  * **El papel del ResourceScheduler:**
    * Consulta la VRAM reservable (`local_vram_budget_gb` − margen de seguridad).
    * Decide el plan seguro: `parallel` (si todos caben a la vez), `waves` (por turnos solapados) o degradación a `sequential`.
    * Memoria Unificada (Apple Silicon / APU): Soporte de `unified_memory_budget_gb` para compartir RAM y VRAM.
  * **Cero simulación:** En la pantalla Comparación solo se muestra solapamiento si los timestamps `started_at` y `completed_at` se cruzaron en la realidad.
* **Pantalla de la App a mostrar:**
  * **Pantalla de Comparación** (`/dashboard/comparison`): Panel central con resumen de planificación:
    * `Plan solicitado`: parallel
    * `Plan efectivo`: waves
    * `Solapamiento`: observado
  * Carriles de tiempo (*timeline-block*) de proponentes y árbitro.
* **Guion Locutado:**
  > *"Si disponemos de una máquina potente o varios modelos ligeros, podemos utilizar el preset 'slow'. Aquí el planificador de recursos calcula la memoria libre antes de dar un solo paso. Si caben dos modelos a la vez, los lanza en paralelo; si no, organiza una ejecución por oleadas o 'waves'.*  
  > *Fijaos en la pantalla de Comparación: esto es transparencia de ingeniería. En el resumen vemos el plan solicitado, el plan que el broker concedió de forma segura y si hubo solapamiento real observado. Los carriles temporales no son una animación CSS bonita: representan los milisegundos exactos de inicio y fin registrados en SQLite. Además, cada carril nos desglosa los tokens consumidos y el coste monetario de esa llamada individual."*
* **Acción en la Demo en Pantalla:**
  * Señalar con el cursor los bloques `Plan solicitado`, `Plan efectivo` y la barra de tiempo del árbitro, destacando que el árbitro solo arranca tras cruzar la barrera de finalización de todos los proponentes.

---

### Diapositiva 4.4 — Caso de Uso 11: Resiliencia de Quórum y Sustitución de Árbitro
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 11 — Resiliencia: "Unas Propuestas Pagadas Valen Más que un Error"
  * **Mecanismos de tolerancia a fallos:**
    * **Caída de un proponente:** Si había 3 proponentes y uno cae (ej. timeout o OOM), la tarea continúa si se mantiene el quórum mínimo (≥ 2) y queda reflejado como `skipped_proposers`.
    * **Fallo del árbitro:** Si el árbitro falla, el broker prueba con un árbitro alternativo; si tampoco sintetiza, entrega la mejor propuesta directamente marcando `consensus.synthesized: false`.
* **Pantalla de la App a mostrar:**
  * [Detalle de Tarea](file:///c:/Procesos/AI_Broker/app/templates/fragments/task_detail_body.html) con alerta amarilla de advertencia:
    * Badge: `Árbitro sustituido` o `Consenso sin síntesis`.
    * Lista de modelos omitidos durante el consenso (`task_result.skipped_proposers`).
* **Guion Locutado:**
  > *"Imaginemos que enviamos un debate complejo que tarda 30 segundos en completarse. Los tres proponentes han terminado sus respuestas con éxito, pero en el último segundo el modelo árbitro falla por un corte de red o un timeout. En otros sistemas, toda la tarea se descartaría y devolvería un error 500.*  
  > *AI Broker aplica una regla de oro de la ingeniería: unas propuestas que ya se han calculado y pagado valen más que un fallo. Si el árbitro falla, el broker intenta una segunda síntesis con otro modelo de respaldo; y si ninguno puede sintetizar, el sistema rescata la mejor propuesta disponible y la entrega, dejando un aviso claro en la interfaz (`consensus.synthesized: false`). Nunca se tira trabajo útil a la basura."*
* **Acción en la Demo en Pantalla:**
  * Mostrar el detalle de una tarea donde aparezca la alerta amarilla `Avisos del consenso` o `Modelos omitidos durante el consenso`.

---

### Diapositiva 4.5 — Caso de Uso 12: Consenso Multironda Condicionado por Juez
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 12 — Consenso de Doble Ronda (`max_rounds: 2`)
  * **Dinámica de la segunda ronda:**
    * Tras la Ronda 1, un juez evalúa la síntesis y asigna una puntuación de confianza (0.0 a 1.0).
    * Si la puntuación supera el umbral, la tarea finaliza y se entrega (ahorrando tiempo y coste).
    * Si la confianza es baja, se dispara la **Ronda 2**: los proponentes reciben la primera síntesis para contrastarla y emitir enmiendas, y el árbitro publica la conclusión definitiva.
    * Invariante de seguridad: La segunda ronda jamás puede degradar el resultado de la primera.
* **Pantalla de la App a mostrar:**
  * **Detalle de la Tarea**: Alerta azul `consensus.round_gate` ("Segunda ronda de consenso: la síntesis puntuó por debajo del umbral").
* **Guion Locutado:**
  > *"Para peticiones de máxima exigencia, AI Broker admite hasta dos rondas de consenso. Sin embargo, la segunda ronda no se ejecuta a ciegas: un juez evalúa la primera síntesis. Si la respuesta ya es sólida y supera el umbral de calidad, el broker se detiene y ahorra recursos.*  
  > *Solo si el juez detecta contradicciones o baja certeza, se activa la segunda vuelta. Como vemos en el panel, el evento `consensus.round_gate` documenta exactamente la nota obtenida y el motivo por el cual los proponentes volvieron a reunirse para revisar la síntesis."*
* **Acción en la Demo en Pantalla:**
  * Señalar en el detalle de la tarea el banner de segunda ronda y el desglose de invocaciones adicionales.

---

### Resumen del Módulo 3 y 4 para el Cierre del Vídeo
* **Puntos clave de recapitulación:**
  1. La ingesta transforma documentos en Markdown sin bloquear la cola de inferencia.
  2. Las imágenes nunca se sustituyen por descripciones si hay modelos con visión disponibles.
  3. Map-Reduce previene el desbordamiento de contexto sin truncar información en silencio.
  4. Mixture of Agents aporta deliberación con sandboxes XML anti-inyección.
  5. El comparador forense demuestra el solapamiento temporal real en GPU sin simular métricas.
  6. La tolerancia a fallos del árbitro asegura que ninguna inferencia pagada se desperdicie.
