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

    tokens = estimate_tokens(request.content.transcript)

    preferred = request.profile.preferred_model

    

    # Prioridad 1: Modelo preferido disponible

    if is_model_available_with_vram(preferred):

        return preferred

    

    # Prioridad 2: Fallback basado en tokens y recursos

    if tokens > 15000:  # Contenido muy largo

        if has_available_vram() and request.quality_priority == "high":

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

│  🧠 Neural Gateway Broker    [Modelos][Config][Logs]        │

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

  "result_markdown": "# Cómo Configurar Ollama...\\n\\n## Resumen...",

  "processing_time_seconds": 45.2,

  "tokens_input": 8400,

  "tokens_output": 2100,

  "cost_usd": 0.001176,

  "metadata": {

    "model_tier": "local",

    "fallback_used": false,

    "chunk_strategy": "single_pass"

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

async def call_deepseek_api(prompt: str, max_tokens: int):

    headers = {

        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",

        "Content-Type": "application/json"

    }

    

    payload = {

        "model": "deepseek-chat",

        "messages": [

            {"role": "system", "content": system_prompt},

            {"role": "user", "content": prompt}

        ],

        "temperature": 0.3,

        "max_tokens": max_tokens

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

    

    return response.json()["choices"][0]["message"]["content"]

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

El Broker acepta prompts y contenido opaco. No genera frontmatter, no clasifica temas y no contiene lógica específica de YouTube u Obsidian. Los modelos cloud instalados mediante Ollama se descubren y ejecutan a través del mismo endpoint de Ollama; no existe un conector `ollama_cloud` separado.

### API asíncrona y durable

- `POST /api/v1/tasks` valida el objeto completo contra el contrato Broker v1 antes de persistir. Solo después devuelve `202` con `task_id`, `status: "queued"` y URLs de consulta/cancelación.
- Una petición que no cumple el 100% del esquema se rechaza inmediatamente con `422 CONTRACT_VALIDATION_FAILED`, lista estructurada de campos y ningún efecto en SQLite o la cola.
- `GET /api/v1/tasks/{task_id}` devuelve el estado actual y, en estado terminal, resultado o error.
- `DELETE /api/v1/tasks/{task_id}` solicita cancelación idempotente.
- `GET /api/v1/models`, `GET /api/v1/queue`, `PATCH /api/v1/queue`, `GET /api/v1/usage` y `GET /health` soportan las dos interfaces.
- `PATCH /api/v1/queue` recibe la lista completa de `task_id` pendientes en el nuevo orden. No permite reordenar tareas activas.

La base `state/broker.db` almacena tareas, orden, intentos, uso por proveedor y eventos. El servidor se ejecuta con un solo worker Uvicorn y un único slot global `max_active_llm_tasks: 1`.

### Planificador serial y no bloqueo de la API

- La API acepta y persiste tareas aunque exista una generación LLM en curso; responder `202` no consume el slot de ejecución.
- Solo una tarea puede estar en una fase que invoque un LLM. La limitación es global y también se aplica si las tareas usarían modelos o proveedores distintos.
- El dispatcher no crea varias tareas de generación. Espera a que la tarea activa sea terminal antes de reclamar la siguiente por orden de cola.
- Los chunks de una misma tarea también se procesan uno a uno y la síntesis comienza después del último chunk.
- Una generación lenta permanece activa; las tareas siguientes permanecen `queued`, mientras el dashboard, el polling, el alta de nuevas tareas y la cancelación siguen respondiendo.
- No se permite configurar un valor mayor que uno en el MVP. Un valor distinto provoca error de configuración al arrancar.

### Enrutamiento y contexto

1. Respetar `preferred_model` cuando esté disponible y soporte la ventana necesaria.
2. Si no está disponible y `fallback_allowed` es falso, terminar con `MODEL_UNAVAILABLE`.
3. Si se permite fallback, elegir primero un modelo Ollama compatible; después un proveedor externo dentro de `max_cost_usd` y del presupuesto mensual.
4. Los precios se leen de configuración y el coste estimado se reserva transaccionalmente antes de lanzar una petición externa.
5. Para entradas que no caben, dividir por límites naturales con solape configurable, ejecutar `chunk_prompt` y combinar con `synthesis_prompt`. Nunca truncar silenciosamente.

El progreso es por fases (`queued`, `routing`, `chunking`, `generating`, `synthesizing`, `completed`) y, cuando aplique, `chunk_current/chunk_total`; no se inventa un porcentaje de tokens restantes.

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

### Recuperación y pruebas

- Al arrancar, devolver tareas `processing` a `queued` incrementando `attempt`, salvo cancelaciones confirmadas.
- Antes de aceptar tráfico, ejecutar limpieza segura de modelos residuales y reconciliar el lease de VRAM.
- Probar reinicio, cancelación, reordenación, Ollama offline, SQLite sin escritura, VRAM insuficiente, fallo de descarga, presupuesto agotado, contrato inválido, contenido largo y varias tareas encoladas verificando que solo una ejecuta llamadas LLM.
