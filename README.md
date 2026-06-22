# AI Broker — Neural Gateway Service

Gateway inteligente de inferencia para LLMs locales (Ollama) y externos (DeepSeek). Enrutamiento dinámico, cola serial, dashboard HTMX, control de presupuesto y health checks proactivos.

## Arquitectura

```
 Cliente HTTP ──► AI Broker ──► Ollama (local)
                      │         ├── llama3.1:70b
                      │         ├── llama3.1:8b
                      │         └── qwen2.5:72b
                      │
                      ├──► DeepSeek API
                      │
                      └──► Dashboard Web (FastAPI + HTMX)
```

## Stack

| Capa | Tecnología |
|------|------------|
| Backend | FastAPI (Python 3.10+) |
| Frontend | Jinja2 + Tailwind CSS + HTMX |
| Cliente HTTP | httpx async |
| Logging | structlog |
| Persistencia | SQLite + WAL journal |
| LLMs locales | Ollama |
| LLMs externos | DeepSeek API |
| Seguridad | keyring (Windows Credential Manager) |

## Funcionalidades

- **Enrutamiento inteligente**: selecciona el modelo óptimo según tamaño de inferencia, VRAM, coste y preferencias del cliente
- **Cola serial estricta**: un solo slot LLM global, aceptación asíncrona (`202 Accepted`), reordenación y cancelación de tareas
- **Dashboard en tiempo real**: polling HTMX cada 3s, cola Kanban, monitor de VRAM y presupuesto
- **Autodescubrimiento de modelos**: consulta dinámica a Ollama `/api/tags` con metadata enriquecida
- **Control de presupuesto DeepSeek**: tracking de coste y alertas al 80% del límite mensual
- **Health supervisor**: `HealthSupervisor` independiente con checks de SQLite, Ollama, VRAM, disco y proveedores externos
- **Servicio Windows**: instalación con inicio automático y reinicio tras fallo
- **Logging estructurado**: `structlog` con trazabilidad por task_id
- **Recuperación al arranque**: tareas `processing` se devuelven a `queued` con incremento de `attempt`

## API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/v1/tasks` | POST | Crear tarea (validación Pydantic, devuelve 202) |
| `/api/v1/tasks/{id}` | GET | Estado y resultado de tarea |
| `/api/v1/tasks/{id}` | DELETE | Cancelación idempotente |
| `/api/v1/models` | GET | Modelos disponibles con estado |
| `/api/v1/queue` | GET | Snapshot de la cola |
| `/api/v1/queue` | PATCH | Reordenar tareas pendientes |
| `/api/v1/usage` | GET | Uso mensual por proveedor |
| `/health` | GET | Estado detallado de dependencias |
| `/health/live` | GET | Liveness del proceso y event loop |
| `/health/ready` | GET | Readiness de SQLite y dispatcher |

### Contrato de tarea

```json
{
  "task_id": "proc_20240620_143022_dQw4w9WgXcQ",
  "status": "completed",
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

## Estados de progreso

```
queued → routing → generating → completed
```

## Algoritmo de enrutamiento

Matriz de decisión que prioriza el modelo preferido del cliente, con fallbacks progresivos:

| Condición | Acción |
|-----------|--------|
| Modelo preferido disponible con VRAM suficiente | Usar preferido |
| > 15k tokens, VRAM disponible, calidad alta | Modelo local grande |
| > 15k tokens, sin VRAM, presupuesto disponible | deepseek-chat |
| > 15k tokens, sin VRAM ni presupuesto | Encolar para después |
| 8k – 15k tokens | llama3.1:8b |
| < 8k tokens | Modelo más rápido disponible |

## Dashboard

- Panel de modelos disponibles (locales y externos) con estado
- Cola Kanban: Pendiente / Procesando / Hecho
- Tarea activa con barra de progreso, modelo asignado y botón de cancelación
- Monitor de VRAM y presupuesto mensual
- Drag & drop para reordenar cola (HTMX)
- Actualización automática cada 3s

## Contrato normativo del MVP

### Responsabilidad del Broker

El Broker recibe inferencias ya preparadas. Su responsabilidad termina en validar el contrato técnico, encolar, enrutar al modelo/proveedor y devolver la respuesta cruda. No genera prompts, no resuelve placeholders, no divide contenido, no sintetiza resultados y no interpreta respuestas.

### API y persistencia

- Validación Pydantic estricta en `POST /api/v1/tasks`. Una petición inválida se rechaza con `422 CONTRACT_VALIDATION_FAILED` sin efectos en SQLite ni la cola.
- Persistencia en SQLite WAL (`state/broker.db`)
- `POST` devuelve `202` con `task_id`, `status: "queued"` y URLs de consulta/cancelación
- `DELETE` es idempotente

### Planificador serial

- Un solo worker Uvicorn y un slot global `max_active_llm_tasks: 1`
- La API acepta y persiste tareas aunque haya una generación en curso
- Cada tarea equivale a una única inferencia. Chunking, encadenamiento y síntesis son responsabilidad del cliente
- Si la inferencia excede el contexto del modelo, se rechaza con `CONTEXT_LIMIT_EXCEEDED`

### VRAM y cancelación

- Protección global con `asyncio.Semaphore(1)` y lease persistido de tarea/modelo
- Cancelación: cierra la respuesta HTTP, marca `cancel_requested`, envía `keep_alive: 0` y verifica con `/api/ps` antes de entregar el slot
- `keep_alive: 0` nunca se envía sobre un modelo con lease activo

### Seguridad (MVP)

- Sin autenticación entre cliente y broker. Solo LAN privada. CORS desactivado.
- API keys en Windows Credential Manager (`keyring`). Sin claves en SQLite, YAML ni logs.
- Dashboard solo muestra si una clave está configurada y sus últimos 4 caracteres.
- Service Windows con inicio automático y recuperación tras fallo.

### Health supervisor

- SQLite cada 10s, Ollama/VRAM/disco cada 30s, proveedores externos cada 5min
- `/health/live`: proceso y event loop vivos
- `/health/ready`: `200` solo si SQLite y dispatcher pueden aceptar tareas
- Ollama o proveedor caído degrada el servicio pero no impide encolar

### Recuperación

- Al arrancar: limpieza de modelos residuales, reconciliación de leases, tareas `processing` a `queued`

## Inicio rápido

```bash
python -m venv venv
venv\Scripts\activate
pip install fastapi uvicorn httpx pyyaml jinja2 python-multipart structlog psutil python-dotenv keyring
uvicorn app.main:app --host 192.168.1.x --port 8080 --workers 1
```

Ver [Agent_AI_Broker.md](Agent_AI_Broker.md) para el diseño completo y [Deployment_Guide.md](Deployment_Guide.md) para el despliegue normativo.

## Licencia

MIT
