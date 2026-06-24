# Agent: AI Broker (Neural Gateway Service)

> **Precedencia:** la sección `Contrato Normativo del MVP` define la API, persistencia y operación obligatorias.



## Stack Tecnológico del Broker



**Backend:** FastAPI (Python 3.10+)

**Frontend:** Jinja2 Templates + Tailwind CSS (CDN) + HTMX (CDN)

**HTTP Client:** httpx (async, para Ollama y APIs externas)

**Tema:** Oscuro por defecto con Tailwind Dark Mode



## Responsabilidades Arquitectónicas



1. **Autodescubrimiento de Modelos:** Consulta dinámica a Ollama local

2. **Enrutamiento Inteligente:** Selección óptima basada en VRAM, coste, y calidad

3. **Gestión de Cola:** Aceptación asíncrona y procesamiento estrictamente serial con cancelación manual

4. **Control de Presupuesto:** Monitorización de gasto en APIs externas

5. **Dashboard Interactivo:** Gestión visual en tiempo real con HTMX



## Algoritmo de Enrutamiento Inteligente



### Matriz de Decisión

```python

def select_optimal_model(request):

    tokens = estimate_inference_tokens(request.inference)

    preferred = request.routing.preferred_model

    

    # Prioridad 1: Modelo preferido disponible

    if is_model_available_with_vram(preferred):

        return preferred

    

    # Prioridad 2: Fallback basado en tokens y recursos

    if tokens > 15000:  # Contenido muy largo

        if has_available_vram() and request.routing.quality_priority == "high":

            return select_largest_available_local()

        elif within_budget_limit("deepseek"):

            return "deepseek-chat"

        else:

            return queue_for_later_processing()

    

    elif tokens > 8000:  # Contenido medio

        return "llama3.1:8b"  # Modelo rápido local

    

    else:  # Contenido corto

        return select_fastest_available()



def estimate_tokens(text):

    # Aproximación: 1 token ≈ 4 caracteres en español

    return len(text) // 4

```



### Control Matemático de Presupuesto DeepSeek



**Fórmula de Coste por Request:**

$$\\text{Coste Total} = \\frac{\\text{Tokens Input} \\times 0.14}{1{,}000{,}000} + \\frac{\\text{Tokens Output} \\times 0.28}{1{,}000{,}000}$$



**Control de Presupuesto:**

$$\\text{Presupuesto Restante} = \\text{Budget Mensual} - \\sum_{i=1}^{n} \\text{Coste Request}_i$$



**Condición de Rechazo:**

$$\\text{Presupuesto Restante} < (\\text{Coste Estimado} \\times 1.5)$$



## Dashboard Web (FastAPI + HTMX)



### Layout Principal del Dashboard

```

┌─────────────────────────────────────────────────────────────┐

│  🧠 Neural Gateway Broker [Modelos][Probador][Config][Logs] │

│  🟢 Sistema Online │ 5 req/día │ DeepSeek: 0.12€/5€        │

├──────────────────────────────┬──────────────────────────────┤

│  MODELOS DISPONIBLES         │  COLA DE PROCESAMIENTO       │

│                              │                              │

│  LOCAL (Ollama)              │  ┌──────┬──────────┬──────┐  │

│  ● llama3.1:70b  🟢 Libre   │  │PEND. │PROCESANDO│HECHO │  │

│  ● llama3.1:8b   🟡 En uso  │  ├──────┼──────────┼──────┤  │

│  ● qwen2.5:72b   🟢 Libre   │  │  ●   │  ●━━━►   │  ✓   │  │

│                              │  │  ●   │          │  ✓   │  │

│  EXTERNAS                    │  │  ●   │  ●━━━►   │  ✓   │  │

│  ● deepseek-chat 🟢 Online  │  └──────┴──────────┴──────┘  │

│                              │                              │

│  VRAM: ████████░░ 52GB/64GB │  [Drag & Drop habilitado]    │

├──────────────────────────────┴──────────────────────────────┤

│  TAREAS ACTIVAS                                             │

│  🔵 req_001 "Cómo configurar Ollama..."                    │

│     llama3.1:70b │ 8.4k tokens │ ████████░░ 68% │ [⏹ Parar]│

│  SIGUIENTE EN COLA: req_002 "Estrategias Trading..."       │

│     deepseek-chat │ 12.1k tokens │ estado: queued           │

└─────────────────────────────────────────────────────────────┘

```



### Funcionalidad Drag & Drop con HTMX

```html

<!-- Cola que se actualiza cada 3 segundos -->

<div hx-get="/dashboard/fragments/queue" 

     hx-trigger="every 3s" 

     hx-target="#queue-container">

  <div id="queue-container">

    <!-- Contenido actualizado automáticamente -->

  </div>

</div>



<!-- Drag & Drop para reordenar cola -->

<div class="task-card bg-gray-800 p-4 rounded cursor-move" 

     draggable="true"

     hx-patch="/api/v1/queue"

     hx-trigger="drop">

  <h3 class="font-semibold">{{ task.title }}</h3>

  <p class="text-sm text-gray-400">{{ task.model }} • {{ task.estimated_time }}</p>

</div>

```

### Probador de Prompts — fase 5

El dashboard incluirá una vista `Probador` con dos entradas mutuamente exclusivas:

- **Prompt libre:** el texto se conserva exactamente en `content.prompt`.
- **JSON:** se valida únicamente la sintaxis y se conserva el texto original; el JSON es contenido para el LLM, no un contrato administrativo ni datos que el Broker deba interpretar.

La ejecución ofrece:

- `single` contra una referencia exacta `provider/deployment/model`;
- `mixture_of_agents/fast` manual, con uno a cinco proponentes, roles y árbitro seleccionados explícitamente;
- controles de generación, formato/schema, privacidad, cloud, fallback, timeout y coste.

El probador debe construir un `TaskCreateRequest` v2 y usar `POST /api/v1/tasks`. Queda prohibido que una ruta HTMX o componente del dashboard invoque directamente Ollama o DeepSeek. Sus tareas comparten cola, slot serial, persistencia, cancelación, contexto, VRAM y presupuesto con el resto del sistema.

El resultado se muestra raw y escapado, junto con uso, modelo efectivo, fallback y metadata de consenso. El historial procede de SQLite y sobrevive a recargas o reinicios. La especificación operativa está en `docs/Prompt_Tester.md`.



## API Endpoints del Broker



### Core Processing API

```python

@app.post("/api/v1/extract")

async def extract_knowledge(request: ExtractRequest):

    # Enrutar a modelo óptimo

    # Procesar con LLM seleccionado

    # Retornar resultado estructurado



@app.get("/api/v1/models")

async def list_available_models():

    # Consultar Ollama local dinámicamente

    # Añadir APIs externas configuradas

    # Retornar lista completa con estados



@app.get("/api/v1/queue")

async def get_queue_status():

    # Snapshot actual de la cola

    # Tareas pendientes, procesando, completadas

    # Métricas de rendimiento



@app.delete("/api/v1/queue/{task_id}")

async def abort_task(task_id: str):

    # Cancelar request HTTP activo

    # Liberar VRAM forzando descarga de modelo

    # Actualizar estado de cola

```



### Respuesta Estándar del Broker

```json

{

  "task_id": "proc_20240620_143022_dQw4w9WgXcQ",

  "status": "success",

  "model_used": "llama3.1:70b",

  "assistant_content": "Respuesta del modelo sin interpretación del Broker...",

  "processing_time_seconds": 45.2,

  "tokens_input": 8400,

  "tokens_output": 2100,

  "cost_usd": 0.001176,

  "metadata": {

    "model_tier": "local",

    "fallback_used": false,

    "inference_kind": "chat"

  }

}

```



## Autodescubrimiento de Modelos



### Consulta Dinámica a Ollama

```python

async def discover_ollama_models():

    try:

        response = await httpx.get(f"{OLLAMA_URL}/api/tags")

        models = response.json()["models"]

        

        enriched_models = []

        for model in models:

            # Enriquecer con metadata adicional

            enriched_models.append({

                "name": model["name"],

                "size_gb": model["size"] / (1024**3),

                "modified_at": model["modified_at"],

                "status": "available",

                "context_window": get_context_window(model["name"]),

                "estimated_vram": estimate_vram_usage(model["size"])

            })

        

        return enriched_models

    except httpx.RequestError:

        return []  # Ollama offline

```



### Gestión de VRAM y Cancelación de Tareas

```python

async def abort_task_and_free_vram(task_id: str):

    task = get_active_task(task_id)

    

    if task.model_type == "ollama_local":

        # 1. Cancelar request HTTP activo

        await cancel_http_request(task.request_handle)

        

        # 2. Forzar descarga del modelo para liberar VRAM

        await httpx.post(

            f"{OLLAMA_URL}/api/generate",

            json={

                "model": task.model_name,

                "keep_alive": 0  # Descarga inmediata

            }

        )

        

        logger.info(f"VRAM liberada para modelo {task.model_name}")

    

    # 3. Actualizar estado y notificar dashboard

    move_task_to_queue(task_id, status="aborted")

    await broadcast_queue_update()

```



## Configuración de APIs Externas



### DeepSeek Integration

```python

async def call_deepseek_api(inference, model_name: str):

    headers = {

        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",

        "Content-Type": "application/json"

    }

    

    payload = {

        "model": model_name,

        "messages": inference.messages,

        "temperature": inference.temperature,

        "max_tokens": inference.max_output_tokens

    }

    

    response = await httpx.post(

        "https://api.deepseek.com/chat/completions",

        headers=headers,

        json=payload,

        timeout=180.0

    )

    

    # Tracking de coste

    usage = response.json()["usage"]

    cost = calculate_cost(usage["prompt_tokens"], usage["completion_tokens"])

    update_monthly_usage("deepseek", cost)

    

    return normalize_provider_response(response.json())

```



## Health Checks y Monitorización



### Sistema de Health Checks

- **Ollama Local:** Ping cada 30 segundos a `/api/tags`

- **APIs Externas:** Test de conectividad cada 5 minutos

- **VRAM:** Monitorización continua con alertas en 90%

- **Presupuesto:** Alertas al 80% del límite mensual



### Logging Estructurado

```python

import structlog



logger = structlog.get_logger()



# Ejemplo de log entry

logger.info(

    "task_completed",

    task_id=task_id,

    model_used=model_name,

    processing_time=duration,

    tokens_processed=token_count,

    cost_usd=cost

)

```

## Contrato Normativo del MVP

### Independencia de dominio

El Broker acepta inferencias ya preparadas y contenido opaco. Su responsabilidad funcional termina en recibir, validar el contrato técnico, encolar, enrutar al modelo/proveedor y devolver su respuesta. No genera prompts, no resuelve placeholders, no divide contenido, no sintetiza resultados, no interpreta respuestas y no contiene lógica de fuentes, afirmaciones, YouTube u Obsidian. Los modelos cloud instalados mediante Ollama se descubren y ejecutan a través del mismo endpoint de Ollama; no existe un conector `ollama_cloud` separado.

### API asíncrona y durable

- `POST /api/v1/tasks` valida el contrato v2 antes de persistir. Una creación nueva devuelve `202`; la misma `idempotency_key` con el mismo hash devuelve la tarea existente con `200`; contenido diferente devuelve `409 IDEMPOTENCY_CONFLICT`.
- Una petición que no cumple el 100% del esquema se rechaza inmediatamente con `422 CONTRACT_VALIDATION_FAILED`, lista estructurada de campos y ningún efecto en SQLite o la cola.
- `GET /api/v1/tasks/{task_id}` devuelve el estado actual y, en estado terminal, resultado o error.
- `DELETE /api/v1/tasks/{task_id}` solicita cancelación idempotente.
- `GET /api/v1/models`, `GET /api/v1/queue`, `PATCH /api/v1/queue`, `GET /api/v1/usage` y `GET /health` soportan las dos interfaces.
- `PATCH /api/v1/queue` recibe la lista completa de `task_id` pendientes en el nuevo orden. No permite reordenar tareas activas.

La base `state/broker.db` almacena tareas, claves/hash idempotentes, orden, intentos, uso y eventos. El servidor usa un worker Uvicorn y un solo workflow activo global; el dispatcher consume la cola autónomamente.

### Planificador serial y no bloqueo de la API

- La API acepta y persiste tareas aunque exista una generación LLM en curso; responder `202` no consume el slot de ejecución.
- Solo un workflow Broker está activo globalmente. En `single` realiza una invocación; `mixture_of_agents` puede planificar invocaciones internas acotadas.
- El dispatcher reclama mediante una transición atómica `queued → routing` dentro de `BEGIN IMMEDIATE`. El loop automático y el tick manual comparten esa operación y nunca activan un segundo workflow mientras exista uno activo.
- Una tarea `single` equivale a una inferencia. Una tarea `mixture_of_agents` puede realizar consenso técnico interno; el chunking y los workflows de conocimiento siguen siendo externos.
- Una generación lenta permanece activa; las tareas siguientes permanecen `queued`, mientras el dashboard, el polling, el alta de nuevas tareas y la cancelación siguen respondiendo.
- No se permite configurar un valor mayor que uno en el MVP. Un valor distinto provoca error de configuración al arrancar.

### Enrutamiento y contexto

**Fase 4 implementada:** el contrato distingue `chat` y `embedding`; conserva exactamente `content.prompt`, rechaza attachments sin mapeo lossless, filtra modelos por capacidad/contexto y normaliza una respuesta técnica sin interpretar contenido de negocio.

1. Respetar `preferred_model` cuando esté disponible y soporte la ventana necesaria.
2. Si no está disponible y `fallback_allowed` es falso, terminar con `MODEL_UNAVAILABLE`.
3. Si se permite fallback, elegir primero un modelo Ollama compatible; después un proveedor externo dentro de `max_cost_usd` y del presupuesto mensual.
4. Los precios se leen de configuración y el coste estimado se reserva transaccionalmente antes de lanzar una petición externa.
5. Si la inferencia excede el contexto del modelo, rechazarla antes de ejecutar con `CONTEXT_LIMIT_EXCEEDED` y límites calculados. El Broker no trunca ni divide; el Orchestrator decide cómo reconstruir el workflow.

La cota previa usa bytes UTF-8 de entrada, schema, reserva de salida y margen de plantilla. Ollama embedding se ejecuta con `truncate: false`. Chat entrega `assistant_content`; embedding entrega un único vector numérico. Invocación y resultado terminal `single` se persisten atómicamente antes de que otro workflow pueda ocupar el slot.

El progreso del Broker se limita a `queued`, `routing`, `generating` y `completed`. Fases como extracción, chunking, comparación o síntesis pertenecen al Orchestrator.

### Cancelación y VRAM

- El control de VRAM y el slot serial se implementan antes que enrutamiento, dashboard o proveedores externos.
- Proteger toda llamada LLM con un `asyncio.Semaphore(1)` global y un lease persistido de tarea/modelo.
- Antes de cargar un modelo, consultar `/api/ps`, verificar VRAM disponible y descargar modelos residuales que no tengan lease activo.
- Cancelar la tarea `asyncio` y cerrar la respuesta HTTP en curso.
- Marcar `cancel_requested` antes del efecto y `cancelled` al confirmarlo.
- En `finally`, enviar `keep_alive: 0` para descargar el modelo si `unload_after_task` está activo y confirmar su ausencia mediante `/api/ps` antes de entregar el slot a la siguiente tarea.
- Nunca usar `keep_alive: 0` sobre un modelo con lease activo. Si la descarga falla, marcar el Broker `degraded`, impedir otra carga que exceda la VRAM y reintentar la limpieza.
- Una cancelación repetida devuelve el estado terminal existente.

### Servicio siempre encendido y salud proactiva

- Ejecutar un `HealthSupervisor` independiente del worker y del dashboard.
- Comprobar SQLite y capacidad de escritura cada 10 segundos; Ollama, `/api/ps`, VRAM y espacio en disco cada 30 segundos; proveedores externos configurados cada 5 minutos.
- Persistir cambios de salud y exponer `healthy`, `degraded` o `unavailable` con causa, última comprobación y latencia.
- `/health/live` responde si el proceso y el event loop están vivos; `/health/ready` solo responde `200` si SQLite y el dispatcher pueden aceptar tareas; `/health` devuelve el detalle de dependencias.
- Ollama o un proveedor caído degrada el servicio pero no impide aceptar tareas en cola. SQLite no disponible devuelve readiness `503` e impide aceptar tareas.
- Instalar el Broker como servicio de Windows con inicio automático, reinicio tras fallo y logs persistentes.

### Seguridad operativa del MVP

Por decisión del hilo, no hay autenticación entre las dos máquinas. En consecuencia:

- Escuchar solo en la interfaz LAN seleccionada, nunca publicar el puerto 8080 en Internet.
- CORS desactivado salvo orígenes exactos configurados para el dashboard.
- Las API keys se guardan mediante `keyring` en Windows Credential Manager y nunca en SQLite, YAML o logs. `.env` solo puede aportar una clave inicial para migrarla al almacén seguro.
- El dashboard permite sustituir una clave, pero solo muestra si está configurada y sus últimos cuatro caracteres.
- El Probador de Prompts escapa siempre input y output, no renderiza HTML generado por modelos y nunca muestra secretos o cabeceras. Validar JSON no autoriza a interpretarlo ni transformarlo.

### Recuperación y pruebas

- Al arrancar, devolver tareas `processing` a `queued` incrementando `attempt`, salvo cancelaciones confirmadas.
- Antes de aceptar tráfico, ejecutar limpieza segura de modelos residuales y reconciliar el lease de VRAM.
- Probar reinicio, cancelación, reordenación, Ollama offline, SQLite sin escritura, VRAM insuficiente, fallo de descarga, presupuesto agotado, contrato inválido, contenido largo y varias tareas encoladas verificando que solo una ejecuta llamadas LLM.
