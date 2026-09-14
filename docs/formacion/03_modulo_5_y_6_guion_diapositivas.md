# AI Broker — Guion de Formación y Diapositivas: Módulos 5 y 6

Este documento contiene el guion de producción audiovisual, contenido de diapositivas y pautas de demostración en vivo sobre las pantallas originales de la app para los **Módulos 5 y 6**, completando el itinerario formativo del AI Broker.

---

# Módulo 5: Agentes Autónomos y Sandbox de Código Seguro

* **Duración estimada:** ~15 minutos  
* **Objetivo didáctico:** Comprender el funcionamiento del bucle agéntico (`strategy: agent`), los guardarraíles de seguridad (iteraciones y presupuesto), las skills integradas (`web_search`, `fetch_url` con defensa SSRF, `calculator`, `current_datetime`), la ejecución aislada de Python en contenedores Docker desechables (`run_code`), el cotejo determinista de citas web y la visualización de artefactos e imágenes generadas en el panel.

---

### Diapositiva 5.1 — El Bucle Agéntico y sus Guardarraíles
* **Contenido de la Diapositiva:**
  * **Título:** Estrategia Agente: Razonamiento con Herramientas y Guardarraíles
  * **Flujo del bucle agéntico:**
    ```
    Prompt Cliente ──► [LLM: Decide Tool Call] ──► [Broker ejecuta Skill] ──► [Resultado vuelve al LLM]
                             ▲                                                              │
                             └─────────────────────── Repetir bucle ────────────────────────┘
                                                     (Hasta respuesta final)
    ```
  * **Guardarraíles que protegen el sistema:**
    * `max_iterations` (1 a 20): Evita bucles infinitos.
    * `max_cost_usd`: Límite estricto de gasto acumulado.
    * **Turno de cierre sin herramientas:** Si se agotan las iteraciones, el broker otorga un turno final forzado sin tools para que el modelo redacte su mejor conclusión con lo que ya sabe.
    * **Recorte de contexto:** Si la conversación crece demasiado, el broker poda salidas antiguas de herramientas para no desbordar la ventana del modelo.
* **Pantalla de la App a mostrar:**
  * [Probador de Prompts](file:///c:/Procesos/AI_Broker/app/templates/prompt_tester.html) (Estrategia: `Agente con skills`).
* **Guion Locutado (Voz en off / Presentador):**
  > *"Bienvenidos al Módulo 5. Hasta ahora hemos visto modelos que responden directamente o deliberan entre sí a partir de la información que les damos. Pero muchas veces el modelo necesita interactuar con el mundo: buscar un dato en la web, leer una página externa, hacer un cálculo matemático exacto o ejecutar código para procesar un archivo.*  
  > *La estrategia 'agent' de AI Broker implementa un bucle de llamada a herramientas (tool-calling). Sin embargo, un agente descontrolado puede entrar en bucles infinitos o gastar presupuesto sin límite. Por eso, el broker incorpora guardarraíles estrictos: podemos fijar un tope de iteraciones de 1 a 20, un límite de coste en dólares, y una política inteligente: si el modelo agota sus turnos, no se aborta la tarea con error, sino que se le concede un turno final sin herramientas para que emita su respuesta con los datos que ya recabó."*
* **Acción en la Demo en Pantalla:**
  * En el Probador, seleccionar **Estrategia: Agente con skills**.
  * Mostrar los controles: campo `Modelo del agente`, selector numérico `Máx. iteraciones` y la fila de casillas de verificación de skills.

---

### Diapositiva 5.2 — Caso de Uso 13: Búsqueda Web, Lectura de URLs y Blindaje SSRF
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 13 — Navegación Web Segura y Cotejo de Citas
  * **Skills de red:**
    * `web_search`: Búsqueda directa en DuckDuckGo sin necesidad de API keys de pago.
    * `fetch_url`: Descarga y extracción de texto de páginas web.
  * **Seguridad de Red — Guardia SSRF:**
    * Antes de conectar, resuelve DNS y comprueba la IP destino.
    * Rechaza de forma fulminante IPs privadas, loopback (`127.0.0.1`), intranets locales y el propio puerto del dashboard para impedir que el agente ataque la red interna.
  * **Cotejo Determinista de Enlaces:**
    * Al terminar, el broker compara los enlaces citados en la respuesta contra las URLs consultadas en las herramientas.
    * Si el modelo cita una URL que nunca visitó, el broker la clasifica en `unsupported citations` sin consultar a otro modelo.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts**: Activación de `Búsqueda web` y `Leer URL`.
  * [Detalle de la Tarea](file:///c:/Procesos/AI_Broker/app/templates/fragments/task_detail_body.html): Sección **Actividad del agente** con el desglose de pasos y el widget de citas auditadas.
* **Guion Locutado:**
  > *"Veamos una consulta de investigación en la web. Marcamos 'Búsqueda web' y 'Leer URL' y lanzamos la tarea.*  
  > *Abramos el detalle de la tarea finalizada: en la sección 'Actividad del agente' tenemos el registro paso a paso. En el paso #1 vemos la consulta que el modelo envió al buscador; en el paso #2 vemos la URL que decidió abrir y el extracto de texto que leyó.*  
  > *Y observad este detalle diferencial: el panel de Citas. Los modelos de lenguaje tienen tendencia a alucinar enlaces creíbles que en realidad no existen. AI Broker coteja matemáticamente las URLs que el modelo incluye en su texto contra las que realmente visitó en las herramientas. Si el agente cita una fuente inventada, la interfaz lo resalta de inmediato como 'sin respaldo en lo consultado'. Además, el guardia SSRF garantiza que el modelo jamás pueda ser manipulado para escanear puertos de nuestra red corporativa."*
* **Acción en la Demo en Pantalla:**
  1. Mostrar una tarea de agente ejecutada.
  2. Desplegar los pasos de la lista ordenada `agent-timeline`.
  3. Señalar en la cabecera el resumen de enlaces: *"N enlace(s) citado(s) · todos consultados"* o los enlaces sin respaldo si los hubiera.

---

### Diapositiva 5.3 — Caso de Uso 14: Sandbox Docker: Código IA sin Riesgo
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 14 — Ejecución Aislada de Python (`run_code`)
  * **Fronteras no configurables del Contenedor Docker:**
    * `--network none`: Cero acceso a internet (nada que exfiltrar).
    * Sin volúmenes del host: No puede leer ni escribir en el disco del ordenador.
    * `--read-only` con tmpfs efímeros en RAM (`/work`, `/tmp`).
    * Usuario `nobody` (UID 65534) y `--cap-drop ALL` (sin privilegios de root).
    * Límites duros de RAM, CPU y número de procesos (PIDs).
    * Destrucción automática al terminar (`--rm`).
  * **Imagen estándar:** Incluye `numpy`, `pandas`, `matplotlib`, `openpyxl`.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts**: Casilla `Ejecutar código (sandbox)` activa.
  * **Detalle de la Tarea**: Paso del agente donde aparece el bloque de código Python ejecutado y su salida estándar (stdout).
* **Guion Locutado:**
  > *"Los modelos de inteligencia artificial son excelentes programadores, y muchas veces la forma más precisa de resolver un problema no es pedirle que adivine la respuesta, sino pedirle que escriba un script en Python y lo ejecute: por ejemplo, para calcular una regresión estadística o generar una gráfica.*  
  > *Pero ejecutar código escrito por una IA directamente en tu ordenador Windows sería una temeridad. AI Broker lo ejecuta dentro de una 'habitación acolchada': un contenedor Docker desechable levantado al vuelo sobre WSL2.*  
  > *Las fronteras del sandbox son de máxima seguridad y no son relajables: no tiene red externa, su sistema de archivos es de solo lectura, no tiene acceso a las carpetas del host y se destruye al instante de terminar. El modelo escribe el código, el broker lo ejecuta en la jaula y le devuelve los resultados o los errores de sintaxis para que el modelo se autocorrija."*
* **Acción en la Demo en Pantalla:**
  * En el detalle de la tarea, enfocar un paso con `skill: run_code`.
  * Mostrar el JSON de argumentos con el script generado por el LLM y la respuesta capturada.

---

### Diapositiva 5.4 — Caso de Uso 15: Galería de Artefactos e Imágenes Generadas
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 15 — Artefactos y Galería Visual de Tareas
  * **El problema:** Modelos que generan imágenes o scripts que dibujan gráficos suelen perderse en textos planos del resultado.
  * **La solución en AI Broker:**
    * Los archivos generados se guardan en disco como artefactos independientes vinculados al `task_id`.
    * **La imagen se pinta con tamaño generoso:** Si la tarea produjo un PNG o SVG, la pantalla de detalle lo exhibe en una galería interactiva con enlace al original a resolución completa.
    * Trazabilidad de archivos eliminados: Si la retención de disco limpió el archivo, la fila sigue informando de que existió pero desactiva el enlace roto.
* **Pantalla de la App a mostrar:**
  * [Detalle de la Tarea](file:///c:/Procesos/AI_Broker/app/templates/fragments/task_detail_body.html): Sección **Artefactos** (`artifact-panel`).
  * Galería visual con una imagen generada (`artifact-gallery`) y tabla de ficheros producidos.
* **Guion Locutado:**
  > *"Cuando un modelo o un script genera una imagen —por ejemplo, un gráfico de dispersión creado con matplotlib—, limitarse a mostrar en una tabla 'archivo.png generado' es insuficiente. El operador necesita ver el resultado.*  
  > *En el detalle de la tarea, AI Broker incorpora un panel propio de Artefactos. Como vemos en pantalla, la imagen se dibuja directamente en la interfaz con un tamaño generoso para evaluar si el gráfico es correcto sin necesidad de abrir programas externos. Al hacer clic sobre ella, se abre el original a resolución completa servido por la propia sesión del panel, sin requerir tokens adicionales ni descargas manuales."*
* **Acción en la Demo en Pantalla:**
  * Hacer clic sobre una miniatura de la galería para abrir la imagen generada en una nueva pestaña.

---

# Módulo 6: Operación, Hardware, Catálogo de Modelos y Configuración en Caliente

* **Duración estimada:** ~18 minutos  
* **Objetivo didáctico:** Gestionar el ciclo de vida del broker en producción: supervisar y reordenar la cola de tareas pendientes, realizar búsquedas forenses en el histórico, auditar el catálogo de modelos, ejecutar el borrado seguro en dos pasos de modelos locales, y modificar la configuración completa del archivo `broker_config.yaml` en caliente con revisión previa de cambios.

---

### Diapositiva 6.1 — Gestión Operativa de la Cola de Tareas
* **Contenido de la Diapositiva:**
  * **Título:** Control y Gestión de la Cola Durable
  * **Estados en espera:** `queued`, `waiting_for_memory`, `waiting_for_dependencies`.
  * **Acciones en vivo:**
    * Reordenación posicional: Botones para subir, bajar o enviar al principio (`top`).
    * Congelación inteligente del refresco: Al marcar casillas para cancelar, el autorrefresco se detiene para no descolocar filas bajo el ratón.
    * Cancelación transaccional: Cancelación puntual o en bloque (`scope=pending`) atómica en SQLite.
* **Pantalla de la App a mostrar:**
  * [Pantalla de Tareas](file:///c:/Procesos/AI_Broker/app/templates/tasks.html) (`/dashboard/tasks`).
  * Tabla de tareas pendientes con casillas de selección múltiple y botones de acción.
* **Guion Locutado:**
  > *"Entramos en el Módulo 6, dedicado a la administración operativa. En la pestaña 'Tareas' gobernamos la cola de peticiones.*  
  > *Aquí vemos las tareas que esperan turno: las que acaban de entrar, las que aguardan a que se libere memoria VRAM y las que dependen de que otra tarea termine antes. Si entra una consulta urgente, podemos hacer clic en la flecha 'top' para colocarla en la primera posición.*  
  > *Fijaos en la ergonomía de la interfaz: en cuanto marcamos una casilla para seleccionar varias tareas, el refresco automático de la página se pausa. De este modo, la tabla no cambia de posición mientras tenemos el ratón encima. Y cuando pulsamos en cancelar, toda la selección se cancela en una única transacción en la base de datos, impidiendo que el motor de inferencia capture accidentalmente un trabajo que ya dimos por cancelado."*
* **Acción en la Demo en Pantalla:**
  1. Marcar una casilla de verificación en la cola y señalar cómo aparece el contador de seleccionadas.
  2. Demostrar el botón de reordenación hacia arriba en una tarea pendiente.

---

### Diapositiva 6.2 — Auditoría Forense en el Histórico de Ejecuciones
* **Contenido de la Diapositiva:**
  * **Título:** Búsqueda Forense y Filtros en el Histórico
  * **Dimensiones de filtrado combinables:**
    * Estado (`completed`, `failed`, `cancelled`).
    * Tipo de tarea (`code`, `prose`, `long_context`, `vision`).
    * Proveedor y Modelo exacto.
    * Origen de la petición (`prompt_tester`, API cliente, automatizaciones).
    * Rango de fechas (`desde`, `hasta`) y búsqueda de texto en el contenido.
  * **Persistencia en URL:** Los filtros seleccionados se guardan en la barra de direcciones para auditorías reproducibles.
* **Pantalla de la App a mostrar:**
  * [Histórico de Tareas](file:///c:/Procesos/AI_Broker/app/templates/task_history.html) (`/dashboard/history`).
* **Guion Locutado:**
  > *"En entornos regulados o evaluaciones académicas, necesitamos poder responder a preguntas como: ¿cuánto gastó este modelo el martes pasado? ¿qué peticiones fallaron por timeout? o ¿qué tareas de código se procesaron ayer?*  
  > *En la pestaña 'Histórico' disponemos de un motor de búsqueda sobre las tablas SQLite. Podemos cruzar estados, proveedores, modelos y rangos de fechas, o teclear un término en la caja de búsqueda. Los filtros se conservan en los enlaces de paginación y en la propia URL del navegador, lo que permite guardar consultas o enviarlas como referencia en un informe técnico."*
* **Acción en la Demo en Pantalla:**
  * Seleccionar en el filtro de proveedor `Ollama`, pulsar filtrar y mostrar la lista de tareas resultante con sus tiempos y costes.

---

### Diapositiva 6.3 — Catálogo de Modelos y Sondeo de Compatibilidad en Vivo
* **Contenido de la Diapositiva:**
  * **Título:** Catálogo de Modelos y Sondeos de Capacidades
  * **Jerarquía de evidencia:**
    1. Sondeo real contra el endpoint (micro-llamadas de 1 token persistidas con timestamp).
    2. Capacidades declaradas por el runtime (Ollama `/api/show`).
    3. Catálogo externo [models.dev](https://models.dev) (cacheado localmente, sin claves).
    4. Heurística por nombre.
  * **Chips de filtrado:** `Visión`, `JSON`, `Tools`, `Solo operativos`.
  * **Botón Analizar:** Lanza sondeo puntual contra un modelo y registra compatibilidad (`compatible`, `incompatible`, `error`).
* **Pantalla de la App a mostrar:**
  * [Pantalla de Modelos](file:///c:/Procesos/AI_Broker/app/templates/models.html) (`/dashboard/models`).
  * Hacer clic en los chips de filtrado y mostrar el botón `Analizar` en la columna de compatibilidad.
* **Guion Locutado:**
  > *"Pasemos a la pantalla de 'Modelos'. Aquí tenemos el inventario exhaustivo de todo el hardware y los modelos disponibles en nuestra máquina.*  
  > *Las tarjetas superiores nos resumen cuántos modelos están operativos, cuántos tienen errores temporales y cuáles están cargados en la memoria de la tarjeta gráfica en este mismo segundo.*  
  > *Fijaos en los chips de filtrado rápido: podemos pulsar en 'Visión', 'JSON' o 'Tools' para aislar exactamente los modelos capacitados para cada tarea. Y si instalamos un modelo nuevo en Ollama o en un servidor compatible con OpenAI, no tenemos que adivinar si funciona: pulsamos en el botón 'Analizar'. El broker realiza una comprobación real de 1 token y nos muestra al instante su compatibilidad verificada."*
* **Acción en la Demo en Pantalla:**
  * Activar y desactivar el chip de filtro `Solo operativos` y pasar el cursor sobre la columna `Precio 1M` para enseñar los metadatos sincronizados desde models.dev.

---

### Diapositiva 6.4 — Borrado Seguro de Modelos Locales en Dos Pasos
* **Contenido de la Diapositiva:**
  * **Título:** Borrado Seguro de Modelos: Sin Accidentes ni Referencias Rotas
  * **Mecanismo de seguridad diferencial:**
    * **Confirmación integrada en la fila (dos pasos):** El primer clic no borra; despliega la confirmación dentro de la propia fila indicando los gigabytes exactos que se van a liberar.
    * **Análisis de consecuencias:** El broker comprueba si el modelo está configurado a mano en `broker_config.yaml` o si es el último modelo con capacidad crítica (ej. embeddings o visión), avisando antes de confirmar.
    * **Bloqueo con cola activa:** Si hay tareas encoladas, el borrado se prohíbe para evitar errores inesperados.
    * **Descarga previa de VRAM:** Antes de eliminar blobs en disco, se libera de la memoria GPU.
* **Pantalla de la App a mostrar:**
  * **Modelos** (`/dashboard/models`): Pulsar el botón rojo `Borrar` de un modelo Ollama local.
  * Mostrar el fragmento interactivo de confirmación [model_delete_confirm.html](file:///c:/Procesos/AI_Broker/app/templates/fragments/model_delete_confirm.html).
* **Guion Locutado:**
  > *"Los modelos locales ocupan decenas de gigabytes en nuestro disco de almacenamiento. Llegará el momento de eliminar los que ya no usemos. En la mayoría de interfaces esto se resuelve con un peligroso botón directo o una ventana nativa de confirmación que nadie lee.*  
  > *AI Broker implementa un protocolo de seguridad en dos pasos. Al pulsar 'Borrar' en un modelo local de Ollama, no se abre ningún pop-up genérico: en la propia fila del modelo se despliega un panel de advertencia que calcula cuántos gigabytes se van a recuperar y analiza si ese modelo está referenciado en la configuración del broker o si es el único con visión que nos queda.*  
  > *Si tenemos tareas pendientes esperando turno en la cola, el sistema nos impide borrar para que ninguna tarea quede huérfana. Y cuando confirmamos, el broker primero descarga el modelo de la VRAM y luego le ordena al runtime de Ollama borrar los blobs de disco de forma limpia y consistente."*
* **Acción en la Demo en Pantalla:**
  * Hacer clic en `Borrar` en un modelo local, mostrar el panel de consecuencias con los bytes a liberar y pulsar `Cancelar`.

---

### Diapositiva 6.5 — Configuración en Caliente y Control de Concurrencia SHA-256
* **Contenido de la Diapositiva:**
  * **Título:** Configuración en Caliente: El Formulario `broker_config.yaml`
  * **Zonas temáticas de configuración:**
    * Zona 1: Proveedores (Ollama, DeepSeek, custom OpenAI-compatible con API keys en keyring).
    * Zona 2: Límites operativos, timeouts y presupuestos de VRAM y memoria unificada.
    * Zona 3 y 4: Ajustes del Meta-router y selección adaptativa por tiempo esperado en segundos.
    * Zona 5 y 6: Idoneidad de tareas (`task_affinity`) y sondeo en sombra (*shadow probe*).
    * Zona 7: Ingesta documental, OCR, Whisper y Sandbox Docker.
  * **Seguridad operativa:**
    * Botón **Revisar sin guardar**: Genera una vista previa del diff (*antes* vs. *después*) validado por Pydantic.
    * **Huella SHA-256:** Evita sobrescrituras accidentales si otra pestaña o proceso editó el archivo entre medias.
* **Pantalla de la App a mostrar:**
  * [Pantalla de Configuración](file:///c:/Procesos/AI_Broker/app/templates/fragments/config.html) (`/dashboard/config`).
  * Mostrar las zonas con sus badges de colores y pulsar `Revisar sin guardar` para enseñar la alerta azul de cambios.
* **Guion Locutado:**
  > *"Cerramos la formación en la pantalla de 'Configuración'. Toda la arquitectura de AI Broker se gobierna desde un único archivo: `broker_config.yaml`. Sin embargo, los operadores no necesitan editar código YAML a mano ni reiniciar el proceso para cambiar parámetros cotidianos.*  
  > *El panel organiza la configuración en bloques temáticos codificados por colores: desde los proveedores y los presupuestos de memoria VRAM, hasta los umbrales de compresión o los límites del contenedor Docker.*  
  > *Fijaos en la seguridad: si modificamos un valor —como el margen de VRAM o el nivel de compresión— y pulsamos en 'Revisar sin guardar', el sistema valida la sintaxis completa y nos presenta un listado exacto de los cambios que se van a aplicar. Y si otra persona o script hubiera modificado el archivo en disco mientras teníamos la página abierta, el formulario detecta el cambio en la huella criptográfica SHA-256 y bloquea el guardado para que jamás pisemos configuraciones por error.*  
  > *Al guardar, la configuración se escribe atómicamente y los servicios en memoria aplican los nuevos ajustes en caliente."*
* **Acción en la Demo en Pantalla:**
  1. Navegar por las secciones de configuración.
  2. Modificar un valor numérico de prueba (ej. `vram_safety_margin_gb`).
  3. Pulsar en **Revisar sin guardar** y mostrar la alerta informativa superior con el diff de campos modificados.

---

### Resumen General del Curso y Cierre de la Formación
* **Puntos clave de recapitulación:**
  1. AI Broker unifica la inferencia multi-modelo sin intermediarios externos y con soberanía absoluta de los datos.
  2. Su arquitectura de doble carril protege la GPU mientras procesa PDFs escaneados, imágenes y vídeos de forma desatendida.
  3. El consenso deliberativo (MoA) permite tomar decisiones colegiadas entre especialistas con protección anti-inyección.
  4. Los agentes ejecutan herramientas web protegidas por SSRF y código Python en jaulas Docker desechables.
  5. La interfaz web ofrece telemetría en tiempo real sin métricas inventadas y control en caliente del sistema.
* **Guion de despedida:**
  > *"Con esto completamos la formación técnica de AI Broker. Tenéis ahora el dominio completo de una herramienta de vanguardia, diseñada para ofrecer la máxima potencia de la inteligencia artificial moderna con el rigor, la seguridad y la transparencia que exige la ingeniería de software profesional. ¡Muchas gracias!"*
