# Agent: AI Broker (Neural Gateway Service)

> **Precedencia:** la sección `Contrato Normativo del MVP` define la API, persistencia y operación obligatorias.



## Stack Tecnológico del Broker



**Backend:** FastAPI (Python 3.10+)

**Frontend:** Jinja2 Templates + HTMX con CSS/JS 100% locales (sin CDN)

**HTTP Client:** httpx (async, para Ollama y APIs externas)

**Ingesta de ficheros:** Docling (PDF/OCR), MarkItDown (Office), faster-whisper (audio/vídeo) — imports perezosos

**Sandbox de código:** contenedores Docker desechables (skill `run_code` del agente)

**Tema:** Oscuro por defecto



## Responsabilidades Arquitectónicas



1. **Autodescubrimiento de Modelos:** Consulta dinámica a Ollama local

2. **Enrutamiento Inteligente:** Selección óptima basada en VRAM, coste, y calidad

3. **Gestión de Cola:** Aceptación asíncrona y procesamiento estrictamente serial con cancelación manual

4. **Control de Presupuesto:** Monitorización de gasto en APIs externas

5. **Dashboard Interactivo:** Gestión visual en tiempo real con HTMX

6. **Ingesta de Adjuntos:** Conversión de documentos/imágenes/audio/vídeo a Markdown antes de la inferencia

7. **Ejecución Aislada:** Código generado por modelos ejecutado en sandbox Docker sin red ni acceso al host



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

> **Precedencia de fase 5:** el contrato operativo verificable está en `docs/Phase_5_Dashboard.md`. El layout histórico siguiente es solo una referencia visual y no autoriza valores simulados, múltiples tareas activas, progreso porcentual, tokens en vivo ni estados de modelo que no procedan de una fuente real.



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
- `mixture_of_agents/fast|slow` manual, con uno a cinco proponentes, etiquetas de rol y árbitro seleccionados explícitamente; `fast` es serial y `slow` admite paralelismo interno acotado;
- controles de generación, formato/schema, privacidad, cloud, fallback, timeout y coste.

El probador debe construir un `TaskCreateRequest` v2 y usar `POST /api/v1/tasks`. Queda prohibido que una ruta HTMX o componente del dashboard invoque directamente Ollama o DeepSeek. Sus tareas comparten cola, controlador de admisión, persistencia, cancelación, contexto, VRAM y presupuesto con el resto del sistema.

El resultado se muestra raw y escapado, junto con uso, modelo efectivo, fallback y metadata de consenso. El historial procede de SQLite y sobrevive a recargas o reinicios. La especificación operativa está en `docs/Prompt_Tester.md`.

Estado actual (julio 2026): implementado, incluyendo estrategia `agent` con casillas de skills (con "Ejecutar código (sandbox)" visible solo con el sandbox activo), proponentes con skills, selector de compresión por prueba, y casillas de **ficheros adjuntos** (los ficheros `ready` de la ingesta se adjuntan como `broker_file` con sus tokens estimados a la vista).



## API Endpoints del Broker



### Core Processing API (endpoints reales)

| Endpoint | Método | Uso |
|----------|--------|-----|
| `/api/v1/tasks` | POST | Crear tarea (`202`; replay idempotente `200`; conflicto `409`) |
| `/api/v1/tasks/{id}` | GET / DELETE | Estado y resultado / cancelación idempotente |
| `/api/v1/tasks/{id}/tool_results` | POST | Resolver tools del cliente (`waiting_for_tools`) y reanudar |
| `/api/v1/files` | POST | Subir adjunto (multipart, `202`, dedupe SHA-256) |
| `/api/v1/files/{id}` | GET | Estado de conversión, metadatos y `tokens_estimate` |
| `/api/v1/files/{id}/markdown` | GET | Markdown resultante cuando `status=ready` |
| `/api/v1/queue` | GET / PATCH | Snapshot de cola / reordenar pendientes |
| `/api/v1/models` | GET | Catálogo con compatibilidad, `features` sondeadas y `catalog` |
| `/api/v1/models/availability` | GET | Disponibilidad operativa por modelo |
| `/api/v1/models/context` | GET | Ventana de contexto y matriz de capacidades |
| `/api/v1/capabilities` | GET | Detección de soporte: contrato, estrategias, flags |
| `/api/v1/usage` | GET | Uso mensual por proveedor |
| `/health` `/health/live` `/health/ready` | GET | Salud, liveness, readiness |



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

El Broker acepta inferencias ya preparadas y contenido opaco. Su responsabilidad funcional termina en recibir, validar el contrato técnico, encolar, enrutar al modelo/proveedor y devolver su respuesta. No genera pasos de negocio, no resuelve placeholders, no divide contenido, no interpreta respuestas y no contiene lógica de fuentes, afirmaciones, YouTube u Obsidian. Si el cliente solicita `mixture_of_agents/fast`, el Broker aplica únicamente el algoritmo técnico versionado que entrega candidatos a un árbitro; esa envoltura no sustituye la orquestación de conocimiento. Los modelos cloud instalados mediante Ollama se descubren y ejecutan a través del mismo endpoint de Ollama; no existe un conector `ollama_cloud` separado.

### API asíncrona y durable

- `POST /api/v1/tasks` valida el contrato v2 antes de persistir. Una creación nueva devuelve `202`; la misma `idempotency_key` con el mismo hash devuelve la tarea existente con `200`; contenido diferente devuelve `409 IDEMPOTENCY_CONFLICT`.
- Una petición que no cumple el 100% del esquema se rechaza inmediatamente con `422 CONTRACT_VALIDATION_FAILED`, lista estructurada de campos y ningún efecto en SQLite o la cola.
- `GET /api/v1/tasks/{task_id}` devuelve el estado actual y, en estado terminal, resultado o error.
- `DELETE /api/v1/tasks/{task_id}` solicita cancelación idempotente.
- `GET /api/v1/models`, `GET /api/v1/queue`, `PATCH /api/v1/queue`, `GET /api/v1/usage` y `GET /health` soportan las dos interfaces.
- `PATCH /api/v1/queue` recibe la lista completa de `task_id` pendientes en el nuevo orden. No permite reordenar tareas activas.

La base `state/broker.db` almacena tareas, claves/hash idempotentes, orden, intentos, uso y eventos. El servidor usa un worker Uvicorn y un solo workflow activo global; el dispatcher consume la cola autónomamente.

### Planificador, concurrencia interna y no bloqueo de la API

- La API acepta y persiste tareas aunque exista una generación LLM en curso; responder `202` no consume el slot de ejecución.
- Solo un workflow Broker está activo globalmente. `single` y `mixture_of_agents/fast` ejecutan estrictamente una invocación a la vez. `mixture_of_agents/slow` puede solapar proponentes dentro de ese workflow; el árbitro espera a la barrera.
- El dispatcher reclama mediante una transición atómica `queued → routing` dentro de `BEGIN IMMEDIATE`. El loop automático y el tick manual comparten esa operación y nunca activan un segundo workflow mientras exista uno activo.
- Una tarea `single` equivale a una inferencia. Una tarea `mixture_of_agents` puede realizar consenso técnico interno; el chunking y los workflows de conocimiento siguen siendo externos.
- Una generación lenta permanece activa; las tareas siguientes permanecen `queued`, mientras el dashboard, el polling, el alta de nuevas tareas y la cancelación siguen respondiendo.
- `max_active_workflows` debe seguir siendo uno. `max_parallel_invocations` puede ser mayor que uno únicamente para `slow` y queda limitado por reservas de VRAM, coste, cuotas y timeout.

### Enrutamiento y contexto

**Fase 4 implementada:** el contrato distingue `chat` y `embedding`; conserva exactamente `content.prompt` en persistencia, filtra modelos por capacidad/contexto y normaliza una respuesta técnica sin interpretar contenido de negocio. Los attachments sin mapeo lossless siguen rechazados; desde el contrato 2.4 existe un mapeo lossless para ficheros ingeridos (`type: "broker_file"`, ver «Novedades del contrato 2.4»): el fichero llega al modelo como Markdown dentro del prompt. Con el servicio opcional de compresión de prompts activo (`prompt_compression` en `broker_config.yaml`, ver [`docs/Prompt_Compression.md`](docs/Prompt_Compression.md)), la copia del prompt de chat que viaja al proveedor se comprime; los embeddings nunca se comprimen. Cada tarea puede fijar su propia compresión con el campo opcional `prompt_compression` del contrato (ver «Novedades del contrato 2.2»).

1. Respetar `preferred_model` cuando esté disponible y soporte la ventana necesaria.
2. Si no está disponible y `fallback_allowed` es falso, terminar con `MODEL_UNAVAILABLE`.
3. Si se permite fallback, elegir primero un modelo Ollama compatible; después un proveedor externo dentro de `max_cost_usd` y del presupuesto mensual.
4. Los precios se leen de configuración y el coste estimado se reserva transaccionalmente antes de lanzar una petición externa.
5. Si la inferencia excede el contexto del modelo, rechazarla antes de ejecutar con `CONTEXT_LIMIT_EXCEEDED` y límites calculados. El Broker no trunca ni divide; el Orchestrator decide cómo reconstruir el workflow.

La cota previa usa bytes UTF-8 de entrada, schema, reserva de salida y margen de plantilla. Ollama embedding se ejecuta con `truncate: false`. Chat entrega `assistant_content`; embedding entrega un único vector numérico. Invocación y resultado terminal `single` se persisten atómicamente antes de que otro workflow pueda ocupar el slot.

En `single`, el progreso se limita a `queued`, `routing`, `generating` y un estado terminal. En `mixture_of_agents/fast|slow` también existen `resource_planning`, `proposing` y `synthesizing` como etapas técnicas internas. `slow` persiste plan, wave y concurrencia observada. Extracción, chunking y workflows de conocimiento pertenecen al Orchestrator.

### Novedades del contrato 2.4 (julio 2026)

`GET /api/v1/capabilities` devuelve `contract_version: "2.4"`. Todos los cambios son aditivos: un cliente de contratos anteriores sigue funcionando sin tocar nada.

**Ingesta de ficheros adjuntos (julio 2026).** Flujo en tres pasos para que una tarea trabaje sobre documentos, imágenes, audio o vídeo:

1. `POST /api/v1/files` (multipart, campo `file`) → `202` con `{file_id, status, sha256, created, status_url}`. El broker valida extensión + magic bytes (`415 INGEST_UNSUPPORTED_FORMAT` / `INGEST_CONTENT_MISMATCH`), tamaño (`413 INGEST_TOO_LARGE`) y deduplica por SHA-256 (`created: false` = ya existía, sin re-conversión).
2. Sondear `GET /api/v1/files/{id}` hasta `status: "ready"` (o `"failed"` con `error.code`: `ENGINE_MISSING`, `CONVERSION_FAILED`, `CONVERSION_TIMEOUT`). En `ready`, `meta` incluye `markdown_chars` y `tokens_estimate` (cota superior conservadora, misma fórmula que el preflight de contexto) para elegir modelo/estrategia antes de crear la tarea; `markdown_url` sirve el documento convertido.
3. Crear la tarea con `content.attachments: [{"type": "broker_file", "metadata": {"file_id": "..."}}]` (o `uri: "broker://files/{id}"`). Ficheros no listos → `409 ATTACHED_FILE_NOT_READY`; inexistentes → `404 ATTACHED_FILE_NOT_FOUND`; fallidos → `409 ATTACHED_FILE_FAILED`. Cualquier otro `type` de attachment sigue rechazado en validación.

En el despacho, el Markdown se inyecta en el prompt dentro de `<attached_document id name>` con neutralización anti-inyección y aviso de contenido no confiable; `request_json` conserva el prompt original del cliente. Con adjuntos, la compresión de prompt pasa a `off` salvo override explícito de la tarea. Formatos soportados en `capabilities.ingestion_formats` (agrupados por tipo); flag `file_ingestion: true`. Detalle completo: [`docs/Phase_7_File_Ingestion.md`](docs/Phase_7_File_Ingestion.md).

**Sandbox de código (skill `run_code`, julio 2026).** La estrategia `agent` (y `proposer_skills` del mixture) acepta la skill `run_code`: el modelo escribe Python y el broker lo ejecuta en un contenedor Docker desechable — sin red, sin ficheros del host, rootfs de solo lectura, usuario sin privilegios, límites de tiempo/memoria/CPU/procesos — devolviendo stdout/stderr/exit code como resultado de tool (el modelo puede corregir y reintentar). Doble opt-in: `run_code` no está en las skills por defecto y requiere `sandbox.enabled` en la configuración del broker; sin sandbox, crear una tarea que la pida devuelve `409 SANDBOX_DISABLED`. Detección: flag `sandbox_run_code` en `capabilities` (y `run_code` aparece en `agent_skills` solo con sandbox activo). Detalle completo: [`docs/Phase_8_Sandbox.md`](docs/Phase_8_Sandbox.md).

**Meta-router de estrategia (`strategy: auto`).** Con `strategy_router.enabled` en la configuración, una tarea con `execution.strategy: "auto"` deja que el broker elija estrategia concreta. La clasificación es **técnica**, no de dominio: necesita datos actuales / cálculo / URL → `agent`; deliberativa (comparar, analizar, prompt largo, datos sensibles) y con presupuesto → `mixture_of_agents`; directa → `single`. La decisión se persiste como evento `strategy.routed` (con señales y motivos), visible en el detalle de la tarea. Router apagado o `strategy: auto` sin él → se resuelve a `single`. Diseñado en tres piezas activables por separado en config: (1) clasificador heurístico, (2) escalado por confianza y (3) aprendizaje adaptativo —las tres implementadas—; `record_cases` guarda los casos desde el principio. Flag `auto_strategy` en `capabilities` (solo true si el router está activo); `auto` aparece en `strategies` solo entonces.

**Escalado por confianza (pieza 2).** Con `strategy_router.confidence_escalation`, cuando el router elige `single`, un modelo juez puntúa la respuesta de 0 a 1; si queda por debajo de `escalation_min_confidence` (0.6 por defecto), la tarea escala a `mixture_of_agents` y el resultado final es el del consenso. Se registran los eventos `strategy.confidence` (puntuación) y `strategy.escalated`. El coste del single + juez se descuenta del presupuesto del mixture. El juez falla abierto: si no devuelve un número usable, no escala. Flag `confidence_escalation` en `capabilities`.

**Aprendizaje adaptativo (pieza 3).** Con `strategy_router.adaptive_learning`, cada tarea auto-enrutada guarda un caso en `routing_cases` (bucket de señales → estrategia elegida → estrategia final → escaló → estado → coste/latencia). Al clasificar, si el bucket de la petición acumula suficientes casos (`learning_min_cases`), la evidencia puede **cambiar la decisión heurística**: si el `single` de ese bucket escaló a menudo (`learning_escalation_threshold`), enruta directo a `mixture` y ahorra el intento single+juez; si la estrategia heurística falla mucho (`learning_failure_threshold`), elige la estrategia con mejor tasa de éxito. La decisión aprendida se marca con `learned: true` en el evento `strategy.routed`. Sin métricas de calidad inventadas: aprende solo de escalados y fracasos observados. Flag `adaptive_strategy_learning` en `capabilities`. Los casos se registran desde la pieza 1 (con `record_cases`) aunque el aprendizaje esté apagado, para no arrancar de cero al activarlo.

**Passthrough de tools del cliente.** La estrategia `agent` acepta `execution.agent.client_tools` (lista de `{name, description, parameters}` — definiciones de function calling que el broker ofrece al modelo pero NO ejecuta). Cuando el modelo llama a una de ellas, la tarea pasa al estado `waiting_for_tools` y su `result.pending_tool_calls` lista las llamadas (`{id, name, arguments}`). El cliente las ejecuta y las resuelve con `POST /api/v1/tasks/{id}/tool_results` con cuerpo `{"tool_results": [{"tool_call_id", "content"}]}` (los ids deben cubrir exactamente los pendientes, si no `409`); el broker reanuda el bucle re-encolando la tarea. Las skills integradas se siguen ejecutando en el broker; solo las tools del cliente pausan. La contabilidad (iteraciones, tokens, coste) se acumula a través de las pausas y `max_iterations` se respeta en total. `client_tool_passthrough: true` en `capabilities` anuncia el soporte. Estado `waiting_for_tools`: no bloquea la cola ni se re-encola en recuperación (espera input externo, no está interrumpida). Flag para clientes: manejar `waiting_for_tools` como estado no terminal en el que hay que actuar.

Contrato 2.3 añadió la estrategia `agent` con skills y los proponentes de mixture con skills.

**Estrategia `agent` (skills técnicas).** Nueva `execution.strategy: "agent"` (solo preset `fast`, solo inference `chat`, sin salida JSON por ahora). El broker ejecuta un loop de tool-calling: el modelo pide herramientas, el broker las ejecuta y le devuelve el resultado como datos, hasta que el modelo responde o se alcanza un guardarraíl. Config en `execution.agent`:

```json
{
  "idempotency_key": "mi-app:456",
  "content": {"prompt": "¿Qué modelos anunció Anthropic esta semana?"},
  "execution": {
    "strategy": "agent",
    "agent": {"skills": ["web_search", "fetch_url"], "max_iterations": 6}
  },
  "model_requirements": {"target_model": {"provider": "lmstudio", "deployment": "local", "model": "..."}}
}
```

Skills disponibles (en `capabilities.agent_skills`): `web_search` (DuckDuckGo, sin clave), `fetch_url` (descarga una URL pública como texto; guardia SSRF que rechaza hosts privados/loopback), `calculator` (aritmética exacta evaluada con AST restringido — sin nombres, atributos ni llamadas) y `current_datetime` (fecha/hora actual en UTC y local).

**Proponentes del mixture con skills.** `execution.proposer_skills` (lista de skills; solo en `mixture_of_agents`, vacío por defecto = comportamiento clásico) hace que cada proponente ejecute un bucle de tool-calling —verificar datos, calcular, consultar fecha— antes de emitir su propuesta; el árbitro sigue sintetizando sin herramientas. En preset `slow` los bucles de los proponentes corren en paralelo (semáforo de VRAM). Cada llamada persiste un evento `agent.tool_call` con el `role` del proponente. Requiere proponentes con tools verificado (mismo fail-clean que la estrategia agent). Flag `proposer_skills: true` en `capabilities`. Guardarraíles: `max_iterations` (1-20) y el `max_cost_usd` global cortan el loop. Soportan chat+tools los proveedores OpenAI-compatible (LM Studio, NVIDIA…), **Ollama** (vía `/api/chat` nativo) y **DeepSeek**. Si el modelo elegido tiene tools verificado como no soportado (sondeo/runtime/catálogo), la tarea se rechaza **antes de encolar** (probador) o falla como primer paso con `MODEL_CAPABILITY_MISMATCH` (API), nunca a mitad del loop; un modelo sin verificar se deja intentar. Cada llamada a skill se persiste como evento `agent.tool_call` (skill, argumentos, tamaño y preview del resultado), visible en el detalle de tarea. El resultado incluye `result.agent = {iterations, stop_reason, skills}` (`stop_reason`: completed / max_iterations / budget_exhausted). Los resultados de skill son datos externos no confiables: el system prompt del agente ignora instrucciones embebidas, misma filosofía que el sandboxing del árbitro.

**Compresión de prompt por tarea.** `POST /api/v1/tasks` acepta el campo opcional de nivel superior `prompt_compression` con valores `"off"`, `"light"`, `"medium"` o `"aggressive"`. Ausente o `null` = usar la configuración global del broker; `"off"` = enviar el prompt tal cual aunque la compresión global esté activa; un nivel concreto sustituye al global (el mínimo de caracteres sigue siendo el de la configuración). El flag `prompt_compression_override: true` en `/api/v1/capabilities` permite detectar el soporte.

```json
{
  "idempotency_key": "mi-app:123",
  "content": {"prompt": "..."},
  "prompt_compression": "off"
}
```

**Registro del prompt que viajó.** Cuando la compresión altera el prompt, el broker persiste un evento `prompt.compressed` en la tarea (payload: `text`, `original_chars`, `compressed_chars`). Ese evento está exento de la poda de eventos: dura lo que la tarea. `content.prompt` original se conserva intacto en `request_json`, como siempre.

**Compatibilidad y capacidades sondeadas por modelo.** El analizador de proveedores OpenAI-compatible clasifica cada modelo en cuatro estados (`compatible`, `incompatible` —veto definitivo por 400/404/422—, `error` —fallo temporal, se reintenta—, `unknown`) y, en los operativos, sondea contra el endpoint real tres capacidades: visión (imagen 1×1), JSON estructurado (`response_format: json_object`) y tools (function calling). Resultado expuesto en la API:

- `GET /api/v1/models`: cada entrada incluye `features` (dict `{vision|json_mode|tools: bool}`; clave ausente = sin sondear o no concluyente) y `features_checked_at`.
- `GET /api/v1/models/context`: la matriz de features refleja el sondeo con prioridad sobre cualquier inferencia por nombre (`generation.json_mode`, `modalities.image_input`, `tools.function_calling` pasan a `supported`/`unsupported` verificados; las notas indican «verificado por sondeo»).
- `GET /api/v1/models/availability`: los `incompatible` no son despachables; pedir un modelo vetado como `target_model` falla en enrutamiento.

**Enriquecimiento con catálogo externo (models.dev).** Con `model_enrichment.enabled: true` en `broker_config.yaml`, el broker descarga una vez al día el catálogo gratuito de models.dev (con caché en disco: sin red se usa la última copia) y cada entrada de `GET /api/v1/models` casada por nombre incluye un dict `catalog`: `vision`/`tools`/`json_mode` declarados, `knowledge_cutoff`, `release_date`, id canónico y — solo cuando el casado es con el proveedor equivalente — `cost_input_per_million`/`cost_output_per_million` de referencia. Jerarquía de evidencia: sondeo real > declarado por el runtime > catálogo externo > heurística por nombre; el catálogo rellena huecos (p. ej. `context_window` cuando la fuente era el default sin verificar, marcado `context_window_source: "catalog"`) y nunca pisa un dato verificado. `GET /api/v1/models/context` aplica la misma jerarquía en su matriz de features.

**Proveedores.** El proveedor `huggingface_local` se retiró (julio 2026): los proveedores válidos son `ollama`, `deepseek` y los `custom` OpenAI-compatible (p. ej. `lmstudio`, `nvidia`). El modo `local_only` de `risk.data_classification` acepta como locales `ollama` y `lmstudio`. Referencias a `huggingface_local` en peticiones fallan con `PROVIDER_NOT_ALLOWED`/`PROVIDER_UNAVAILABLE`.

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

- Escuchar solo en la interfaz LAN seleccionada, nunca publicar el puerto 8765 en Internet.
- CORS desactivado salvo orígenes exactos configurados para el dashboard.
- Las API keys se guardan mediante `keyring` en Windows Credential Manager y nunca en SQLite, YAML o logs. `.env` solo puede aportar una clave inicial para migrarla al almacén seguro.
- La primera entrega del dashboard no permite leer ni sustituir claves. La gestión se mantiene en Windows Credential Manager mediante CLI hasta disponer de autenticación administrativa y auditoría específicas.
- El Probador de Prompts escapa siempre input y output, no renderiza HTML generado por modelos y nunca muestra secretos o cabeceras. Validar JSON no autoriza a interpretarlo ni transformarlo.

### Recuperación y pruebas

- Al arrancar, devolver tareas `processing` a `queued` incrementando `attempt`, salvo cancelaciones confirmadas.
- Antes de aceptar tráfico, ejecutar limpieza segura de modelos residuales y reconciliar el lease de VRAM.
- Probar reinicio, cancelación, reordenación, Ollama offline, SQLite sin escritura, VRAM insuficiente, fallo de descarga, presupuesto agotado, contrato inválido, contenido largo y varias tareas encoladas verificando que solo una ejecuta llamadas LLM.
