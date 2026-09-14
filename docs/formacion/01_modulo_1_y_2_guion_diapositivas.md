# AI Broker — Guion de Formación y Diapositivas: Módulos 1 y 2

Este documento contiene el guion de producción audiovisual, contenido de diapositivas y pautas de demostración en vivo sobre las pantallas originales de la app para los **Módulos 1 y 2**.

---

# Módulo 1: Introducción, Filosofía y Arquitectura del AI Broker

* **Duración estimada:** ~10 minutos  
* **Objetivo didáctico:** Comprender qué problema resuelve el AI Broker, su principio de "cero métricas simuladas", su arquitectura de doble carril con cola durable, la frontera contractual de privacidad y la navegación inicial por el panel en su tema oscuro nativo.

---

### Diapositiva 1.1 — Portada y Propósito
* **Contenido de la Diapositiva:**
  * **Título:** AI Broker — Tu Central Privada de Inferencia de Inteligencia Artificial
  * **Subtítulo:** Puerta de enlace unificada, multi-modelo, auditable y local-first
  * **Puntos clave proyectados:**
    * Un único punto de entrada para modelos locales (Ollama, LM Studio) y cloud (DeepSeek, NVIDIA, OpenAI-compatible).
    * No depende de ninguna nube para funcionar.
    * Cada decisión de enrutamiento y cada métrica se basa en evidencia real medida en tu propia máquina.
    * Trazabilidad completa mediante *Event Sourcing* y persistencia en SQLite.
* **Pantalla de la App a mostrar:**
  * **Pantalla de inicio / Topbar** (`http://127.0.0.1:8765/dashboard`).
  * Mostrar el tema oscuro nativo, la barra lateral con la marca `AI Broker` y el indicador de estado: `Servicio operativo`.
* **Guion Locutado (Voz en off / Presentador):**
  > *"Bienvenidos a esta formación sobre el AI Broker. Imaginemos una empresa o un entorno de ingeniería donde queremos aprovechar lo mejor de la inteligencia artificial: modelos de lenguaje locales que protegen nuestra privacidad, modelos de visión para inspeccionar planos o fotos, y modelos punteros en la nube para razonamientos matemáticos complejos. Normalmente esto genera dispersión, riesgo de fugas de datos y falta de control sobre los costes.*  
  > *El AI Broker nace como una central privada de inferencia. Es un servicio que se ejecuta en tu propio ordenador, no depende de ningún tercero para operar y actúa como el recepcionista inteligente de todas tus peticiones: recibe texto, documentos, imágenes, audio o vídeo, decide qué especialista debe responder o si varios deben debatir entre sí, y entrega el resultado con trazabilidad matemática y forense de cada céntimo y milisegundo invertido."*
* **Acción en la Demo en Pantalla:**
  * Mostrar el navegador en pantalla completa, señalar en la barra lateral los 9 módulos de navegación (`Resumen`, `Tareas`, `Histórico`, `Probador`, `Comparación`, `Modelos`, `Ficheros`, `Enrutamiento`, `Configuración`).

---

### Diapositiva 1.2 — Principios Innegociables: La Evidencia es el Producto
* **Contenido de la Diapositiva:**
  * **Título:** Los Principios Fundamentales del Sistema
  * **Esquema comparativo:**
    * **Cero métricas inventadas:** Si falta un dato se muestra `N/D` con su motivo. Nada de porcentajes de carga estimados ni barras de progreso artificiales.
    * **Toda cifra lleva su procedencia:** Ventana temporal explícita, denominador real y timestamp verificado.
    * **Frontera de datos contractualmente vinculante:** El parámetro `data_classification` bloquea la nube y las herramientas de red a nivel de validación Pydantic, no mediante promesas.
    * **Diseñado para segundo monitor:** Interfaz en tema oscuro templado, legible de reojo, que destaca únicamente lo que requiere la atención del operador.
* **Pantalla de la App a mostrar:**
  * **Panel Resumen** (`/dashboard`).
  * Zoom a las tarjetas de métricas (`metric-grid`) y al widget de **Salud** (`fragments/health.html`).
* **Guion Locutado:**
  > *"En el desarrollo de software actual es habitual encontrar paneles llenos de números ficticios o barras de carga animadas que solo sirven para rellenar la pantalla. AI Broker rompe radicalmente con eso: aquí la evidencia es el producto.*  
  > *Si una métrica no se ha medido, la app muestra 'N/D'. Las latencias p50 y p95 se calculan sobre invocaciones reales ya finalizadas. El consumo de VRAM no es una estimación: es un snapshot directo de la memoria de la GPU consultado al runtime de Ollama con fecha y hora. Y lo más importante: la privacidad no es una declaración de intenciones, sino una frontera algorítmica. Si una tarea es 'Confidencial', el broker hace fallar la petición antes de permitir que un solo byte toque un servidor externo."*
* **Acción en la Demo en Pantalla:**
  * Pasar el cursor por la tarjeta `Latencia p95` destacando el texto *"tareas terminadas"*, y por la tarjeta `Carga de la máquina`, señalando el desglose de los dos carriles del sistema.

---

### Diapositiva 1.3 — Arquitectura del Sistema: Carriles y Concurrencia
* **Contenido de la Diapositiva:**
  * **Título:** Arquitectura Operativa y Carriles de Trabajo
  * **Diagrama visual:**
    ```
    [Cliente / Probador / API] ──► [Cola Durable SQLite] (Event Sourcing)
                                            │
                    ┌───────────────────────┴───────────────────────┐
                    ▼                                               ▼
         [Carril de Inferencia]                         [Carril de Conversiones]
         max_active_workflows: 1                        ingestion.max_concurrent: 1
         Arbitraje estricto de VRAM                    PDF / Audio / Vídeo / OCR
         (No compite con la GPU de inferencia)         (Corre en paralelo sin bloquear)
    ```
  * **Invariantes técnicos:**
    * Un único workflow de inferencia activo a la vez para proteger la GPU.
    * Carril de ingesta documental concurrente e independiente.
    * Peticiones asíncronas con respuesta `202 Accepted` y polling desacoplado.
* **Pantalla de la App a mostrar:**
  * **Panel Resumen** (`/dashboard`) enfocado en la tarjeta `Carga de la máquina` y el panel `Tarea activa`.
* **Guion Locutado:**
  > *"Veamos cómo gestiona el broker la carga física de la máquina. Cuando trabajamos con modelos de lenguaje en local, la memoria de la tarjeta gráfica es el recurso más crítico. Si dos modelos de 14 o 70 mil millones de parámetros intentaran cargar a la vez sin control, la máquina sufriría un cuelgue por falta de VRAM.*  
  > *AI Broker implementa un invariante estricto: solo hay **un workflow de inferencia activo global**. Las peticiones se encolan en SQLite de forma durable, lo que significa que si el servidor o la máquina se reinician, ninguna tarea se pierde. Sin embargo, para que procesar un documento pesado no detenga las respuestas del sistema, existe un segundo carril paralelo: el **carril de conversiones**, que extrae texto y transcribe audio sin consumir el turno de la inferencia."*
* **Acción en la Demo en Pantalla:**
  * Mostrar cómo la tarjeta `Carga de la máquina` desglosa `inferencia 0/1 · conversiones 0/1` y cómo el panel `Tarea activa` avisa explícitamente: *"Solo un workflow puede estar activo"*.

---

### Diapositiva 1.4 — Puesta en Marcha y Autenticación Administrativa
* **Contenido de la Diapositiva:**
  * **Título:** Arranque Seguro, Loopback y Control de Acceso
  * **Puntos clave:**
    * Scripts de ciclo de vida en Windows: `arrancar_ai_broker.bat`, `parar_ai_broker.bat`, `estado_ai_broker.bat`.
    * Enlace loopback por defecto (`127.0.0.1:8765`) con política *Fail-Closed*.
    * Panel protegido por sesión administrativa (`/dashboard/login`):
      * Protección contra fuerza bruta (`LoginThrottle`).
      * Cookie `HttpOnly`, `SameSite=Lax` con caducidad deslizante.
      * Tokens CSRF de doble envío en cada mutación del sistema.
* **Pantalla de la App a mostrar:**
  * **Terminal ejecutando `arrancar_ai_broker.bat`** transicionando a la pantalla de **Login** (`/dashboard/login`).
* **Guion Locutado:**
  > *"Poner en marcha el broker en Windows es tan sencillo como hacer doble clic en `arrancar_ai_broker.bat`. El script valida que el entorno virtual esté íntegro, comprueba que el puerto 8765 esté libre y lanza el servicio.*  
  > *Por seguridad, el sistema arranca en modo 'fail-closed': solo escucha en la interfaz de loopback local. Si se configura un token de administración, nadie en la red puede consultar prompts, resultados ni cambiar configuraciones sin autenticarse. La sesión se gestiona con cookies cifradas y mecanismos anti-CSRF, garantizando que el acceso al panel sea completamente seguro tanto para el operador habitual como en una máquina de evaluación o laboratorio."*
* **Acción en la Demo en Pantalla:**
  * Mostrar brevemente la pantalla de Login con el campo `Token de administración`, introducir la credencial y acceder al panel principal observando la redirección limpia a `/dashboard`.

---

# Módulo 2: Inferencia, Probador de Prompts y Estrategias Básicas

* **Duración estimada:** ~15 minutos  
* **Objetivo didáctico:** Dominar el corazón de la interacción con el broker: la pantalla del Probador de Prompts (`/dashboard/prompt-tester`), el modo `single` exacto, la validación contractual, la compresión reactiva de textos, el uso de esquemas estructurados (JSON Schema) y la estrategia automática (`strategy: auto`).

---

### Diapositiva 2.1 — Anatomía del Probador de Prompts
* **Contenido de la Diapositiva:**
  * **Título:** El Probador de Prompts: Interfaz de Diagnóstico y Validación
  * **Estructura en 4 paneles:**
    1. **Entrada:** Modo Prompt libre o JSON opaco, nivel de compresión y selector de adjuntos.
    2. **Ejecución:** Estrategia (`single`, `mixture_of_agents`, `agent`), selector de modelos con filtrado inteligente.
    3. **Límites y Salida:** Parámetros de generación (temperatura, max tokens, timeout, coste), privacidad y formato.
    4. **Request Validado:** Previsualización en vivo del objeto JSON contractual antes de emitir la tarea.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts** (`/dashboard/prompt-tester`).
* **Guion Locutado:**
  > *"Pasemos a la herramienta más versátil del operador: el Probador de Prompts. Esta pantalla no es un simple chat de juguete: es una consola de ingeniería que construye exactamente las mismas peticiones que emitiría una aplicación externa contra la API REST del Broker.*  
  > *La interfaz está organizada en cuatro bloques estratégicos: a la izquierda introducimos nuestro prompt y configuramos la compresión o los adjuntos; en el centro decidimos la estrategia y el modelo; abajo a la derecha definimos los límites técnicos y de privacidad; y en el cuadrante inferior vemos el 'Request validado', que nos muestra el contrato exacto en formato JSON antes de encolar."*
* **Acción en la Demo en Pantalla:**
  * Realizar un paneo visual por los 4 paneles de la pantalla destacando los títulos y las ayudas integradas.

---

### Diapositiva 2.2 — Caso de Uso 1: Inferencia Directa con Modelo Único (`single`)
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 1 — Consulta Directa con Modelo Exacto
  * **Flujo operativo:**
    * Selección de modelo por identidad completa: `proveedor / deployment / modelo`.
    * Exclusión automática de modelos no operativos (los incompatibles no se muestran en el selector).
    * Acción **Validar** (comprobación sintáctica sin consumo de GPU).
    * Acción **Encolar** (creación durable en SQLite y asignación de ID).
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts:** Formulario relleno con un prompt técnico (ej. *"Explica la diferencia entre semáforos y mutexes en sistemas operativos"*).
  * Desplegable de modelos mostrando la lista filtrada con compatibilidad y capacidades.
  * Alerta verde de `Tarea encolada` con el enlace al ID y estado en vivo.
* **Guion Locutado:**
  > *"Comencemos con el caso de uso más directo: queremos que un modelo concreto responda a nuestra pregunta sin intermediarios. En el selector de estrategia elegimos 'Modelo único'.*  
  > *Al hacer clic en el campo 'Modelo único exacto', se despliega el catálogo. Fijaos en un detalle crucial: solo aparecen modelos operativos cuyo soporte para chat ha sido verificado. Si un modelo en Ollama tiene pesos corruptos o un error de plantilla, el probador lo aparta para evitar que perdamos el tiempo.*  
  > *Escribimos nuestra consulta, ajustamos la temperatura a 0.2 y pulsamos primero en 'Validar'. El sistema construye el payload Pydantic, comprueba que los parámetros sean correctos y nos da el visto bueno. Ahora pulsamos 'Encolar': la tarea entra a la cola durable, recibe un identificador único y un widget dinámico nos informa en tiempo real de su estado."*
* **Acción en la Demo en Pantalla:**
  1. Teclear el prompt en el área de texto.
  2. Filtrar en el combo de modelos escribiendo `llama3` o `qwen`.
  3. Pulsar el botón secundario **Validar**: ver cómo aparece el JSON en la tarjeta de previsualización.
  4. Pulsar **Encolar**: ver aparecer el banner de éxito verde con el ID de tarea (ej. `task_01j7...`).

---

### Diapositiva 2.3 — Caso de Uso 2: Reducción de Costes con Compresión de Prompts
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 2 — Compresión Determinista de Prompts
  * **Mecanismo de ahorro:**
    * Eliminación sistemática de cortesías, saludos, muletillas y relleno conversacional.
    * 3 niveles disponibles: `light` (cortesías), `medium` (+ muletillas), `aggressive` (estilo síntesis caveman).
    * **Preservación estricta:** URLs, direcciones de correo y bloques de código (`fenced code`) se mantienen byte a byte.
    * Vista previa del ahorro en porcentaje antes del envío.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts:** Caja de texto con un prompt humano largo y redundante:
    ```
    Hola, buenos días. ¿Serías tan amable, por favor, de revisar este código y decirme si tiene fugas de memoria? Muchas gracias por tu ayuda:
    ```python
    void* p = malloc(1024);
    ```
    ```
  * Desplegar la tarjeta de **Reducción del prompt** mostrando la insignia verde de ahorro (ej. `-42%`).
* **Guion Locutado:**
  > *"Un porcentaje muy alto de los tokens que enviamos a los modelos de lenguaje está formado por saludos, fórmulas de cortesía y muletillas que no aportan nada al razonamiento y que, sumadas a lo largo de miles de peticiones, disparan la latencia y la factura de la API.*  
  > *AI Broker incluye un motor de compresión determinista en español. Observemos el ejemplo en pantalla: pegamos una consulta cargada de rellenos y seleccionamos nivel 'medium'. Al pulsar 'Validar', la tarjeta de reducción nos muestra un ahorro de más del 40% de caracteres.*  
  > *Lo más importante desde la óptica de ingeniería: el código dentro del bloque Python no se ha tocado en un solo espacio, las URLs no sufren alteraciones y el prompt original queda guardado en la base de datos para cualquier auditoría futura."*
* **Acción en la Demo en Pantalla:**
  * Cambiar el selector de compresión entre `light`, `medium` y `aggressive` y pulsar **Validar** para ver cómo cambia la vista previa interactiva del texto resultante.

---

### Diapositiva 2.4 — Caso de Uso 3: Salidas Estructuradas con JSON Schema
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 3 — Respuestas Estructuradas y Validación de Esquema
  * **Garantía de contrato:**
    * `output_format: json` fuerza la sintaxis JSON en el modelo.
    * Inyección de `json_schema` vinculante.
    * **Detección proactiva:** La interfaz comprueba si el modelo seleccionado tiene verificada la capacidad `json_mode`. Si no la tiene, alerta al operador antes de ejecutar.
* **Pantalla de la App a mostrar:**
  * **Probador de Prompts:** Selector `Formato salida: JSON` activo.
  * Campo de texto `JSON Schema de salida` visible con un esquema de clasificación:
    ```json
    {
      "type": "object",
      "properties": {
        "severidad": {"type": "string", "enum": ["baja", "media", "alta", "critica"]},
        "resumen": {"type": "string"},
        "accion_recomendada": {"type": "string"}
      },
      "required": ["severidad", "resumen", "accion_recomendada"]
    }
    ```
  * Alerta contextual si se selecciona un modelo sin soporte JSON verificado.
* **Guion Locutado:**
  > *"Cuando integramos modelos de IA con sistemas empresariales o bases de datos, no podemos permitirnos que el modelo devuelva prosa libre cuando esperamos datos estructurados.*  
  > *En el Probador seleccionamos formato 'JSON'. Automáticamente se abre el campo para definir el JSON Schema. Aquí establecemos que la respuesta debe ser un objeto con los campos obligatorios severidad, resumen y acción recomendada.*  
  > *Fijaos en la inteligencia de la interfaz: si intentamos emparejar este esquema con un modelo antiguo cuyo sondeo verificó que no soporta 'json_mode', el panel muestra una advertencia amarilla instantánea. El broker previene el fallo antes de enviar la petición."*
* **Acción en la Demo en Pantalla:**
  * Seleccionar formato JSON, pegar el esquema en la caja de texto y verificar cómo el panel "Request validado" incorpora el objeto en `output.json_schema`.

---

### Diapositiva 2.5 — Caso de Uso 4: El Meta-Router Autónomo (`strategy: auto`)
* **Contenido de la Diapositiva:**
  * **Título:** Caso Práctico 4 — Meta-Router: Clasificación y Escalado Autónomo
  * **Las 3 piezas del Meta-Router:**
    1. **Clasificador heurístico determinista:** Analiza señales en la petición (recencia, cálculos matemáticos, presencia de URLs, longitud del texto).
    2. **Escalado por confianza:** Un modelo único responde primero; si un juez evalúa su confianza por debajo del umbral (ej. < 0.75), escala automáticamente a un comité multi-modelo con el presupuesto restante.
    3. **Aprendizaje adaptativo persistido:** Los casos se agrupan en `routing_cases` por huella de señales; con suficiente evidencia empírica, el router corrige la heurística inicial.
* **Pantalla de la App a mostrar:**
  * **Pantalla de Enrutamiento** (`/dashboard/routing`).
  * Tabla **Aprendizaje por tipo de petición** (`buckets`) mostrando columnas: `Tipo de petición`, `Casos`, `Estrategias`, `Escalado de single` y `Recomendación aprendida`.
* **Guion Locutado:**
  > *"¿Qué ocurre si quien emite la petición no sabe de antemano qué modelo o estrategia conviene utilizar? Para eso existe la estrategia 'auto' y el Meta-Router.*  
  > *En lugar de aplicar una regla rígida, el broker combina tres capas de decisión: primero, un clasificador determinista busca señales técnicas en la entrada; segundo, si la respuesta inicial arroja dudas o baja confianza, el broker no se rinde ni entrega una mala respuesta: escala la tarea a un consenso deliberativo de varios modelos.*  
  > *Y tercero, lo que vemos ahora mismo en la pantalla de Enrutamiento: el sistema aprende. En esta tabla vemos los grupos de peticiones por huella de señales. Cuando un tipo de tarea acumula casos suficientes, el router es capaz de corregir su heurística inicial y aplicar la estrategia que históricamente ha demostrado mayor tasa de éxito."*
* **Acción en la Demo en Pantalla:**
  * Navegar en el menú lateral hacia **Enrutamiento** (`/dashboard/routing`).
  * Señalar con el cursor una fila de la tabla donde aparezca una insignia verde de recomendación aprendida.

---

### Diapositiva 2.6 — Inspección Forense: La Pantalla de Detalle de Tarea
* **Contenido de la Diapositiva:**
  * **Título:** Trazabilidad Forense: La Anatomía de una Tarea Finalizada
  * **Elementos auditables en pantalla:**
    * **Resumen técnico:** Modelo exacto utilizado, huella de ejecución (hash de configuración), tokens de entrada/salida y coste en dólares.
    * **Eventos del ciclo de vida:** Banners de decisión (`strategy.routed`, `strategy.escalated`, `prompt.compressed`).
    * **Prompt Original vs. Prompt Comprimido:** Comparador textual desplegable.
    * **Resultado final:** Respuesta en texto o Markdown completamente sanitizada contra inyecciones y XSS.
* **Pantalla de la App a mostrar:**
  * **Detalle de Tarea** (`/dashboard/tasks/{task_id}`).
* **Guion Locutado:**
  > *"Para cerrar este segundo módulo, abramos la pantalla de Detalle de la tarea que acabamos de ejecutar.*  
  > *Aquí es donde el tribunal de evaluación o el auditor de seguridad pueden comprobar el rigor del AI Broker. En el panel superior derecho tenemos el coste monetario exacto en dólares y los tokens persistidos. Abajo podemos desplegar tanto el prompt original que escribió el usuario como el texto comprimido que realmente viajó al modelo.*  
  > *Si el router tomó una decisión autónoma, aquí queda registrada con sus motivos exactos. Y en la tabla de invocaciones podemos ver qué deployment ejecutó la llamada e incluso el hash de huella de configuración, garantizando que sabemos exactamente con qué parámetros se sirvió cada respuesta."*
* **Acción en la Demo en Pantalla:**
  * Hacer clic en una tarea terminada desde la cola o el historial.
  * Desplegar el bloque de `Prompt`, pasar el cursor por la tabla de `Invocaciones` y mostrar el coste y los tokens reales.
