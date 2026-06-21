# 🧠 AI Broker — Neural Gateway Service

Gateway inteligente de procesamiento con LLMs locales (Ollama) y externos (DeepSeek). Enrutamiento dinámico, cola serial, dashboard HTMX y control de presupuesto.

## Arquitectura

```
App ──► AI Broker ──► Ollama (local)
Cliente          │         ├── llama3.1:70b
  HTTP           │         ├── llama3.1:8b
                 │         └── qwen2.5:72b
                 │
                 ├──► DeepSeek API
                 │
                 └──► Dashboard Web
                     (FastAPI + HTMX)
```

## Stack

| Componente | Tecnología |
|------------|------------|
| Backend | FastAPI (Python 3.10+) |
| Frontend | Jinja2 + Tailwind CSS + HTMX |
| Cliente HTTP | httpx async |
| Logging | structlog |
| Persistencia | SQLite + WAL |
| LLMs locales | Ollama |
| LLMs externos | DeepSeek API |

## Funcionalidades clave

- **Enrutamiento inteligente**: selecciona modelo óptimo según tamaño de contenido, VRAM disponible y coste
- **Cola serial** con un solo slot LLM, aceptación asíncrona (`202 Accepted`) y cancelación de tareas
- **Dashboard en tiempo real** con polling HTMX cada 3s, drag & drop de cola y monitorización VRAM
- **Autodescubrimiento de modelos**: consulta dinámica a Ollama `/api/tags`
- **Control de presupuesto** DeepSeek con tracking de coste y alertas al 80%
- **Health checks**: Ollama cada 30s, APIs externas cada 5min, VRAM >90% alerta
- **Logging estructurado** con structlog
- **Recuperación al arranque**: tareas `processing` vuelven a `queued`

## API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/v1/tasks` | POST | Crear tarea (devuelve 202) |
| `/api/v1/tasks/{id}` | GET | Estado de tarea |
| `/api/v1/tasks/{id}` | DELETE | Cancelar tarea |
| `/api/v1/models` | GET | Modelos disponibles |
| `/api/v1/queue` | GET | Estado de la cola |
| `/api/v1/queue` | PATCH | Reordenar cola |
| `/api/v1/usage` | GET | Uso mensual |
| `/health` | GET | Health check |

## Estados de progreso

```
queued → routing → chunking → generating → synthesizing → completed
```

## Dashboard

El dashboard web incluye:
- Panel de modelos disponibles (locales y externos) con estado
- Cola Kanban: Pendiente / Procesando / Hecho
- Tarea activa con barra de progreso, modelo asignado y botón de cancelación
- Monitor de VRAM y presupuesto mensual
- Actualización automática cada 3s

## Algoritmo de enrutamiento

Matriz de decisión que prioriza el modelo preferido del perfil, con fallbacks progresivos según tamaño de contenido:

| Tokens | Condición | Acción |
|--------|-----------|--------|
| Cualquiera | Modelo preferido disponible | Usar preferido |
| > 15k | VRAM disponible y alta calidad | Modelo local grande |
| > 15k | Sin VRAM, presupuesto disponible | deepseek-chat |
| > 15k | Sin VRAM ni presupuesto | Encolar para después |
| 8k – 15k | — | llama3.1:8b (rápido) |
| < 8k | — | Modelo más rápido disponible |

## Contrato normativo del MVP

- API asíncrona con persistencia SQLite (`state/broker.db`)
- Planificador estrictamente serial: `max_active_llm_tasks: 1`
- Enrutamiento respeta `preferred_model`, `fallback_allowed`, `max_cost_usd` y presupuesto mensual
- Chunking por límites naturales con solape configurable, sin truncar silenciosamente
- Cancelación con descarga de modelo (`keep_alive: 0`) solo si ninguna otra tarea activa lo usa
- Sin autenticación entre clientes y broker (decisión del MVP). CORS desactivado. Solo LAN privada.
- API keys solo en `.env`, visibles solo últimos 4 caracteres en dashboard

## Seguridad operativa (MVP)

- Escuchar solo en interfaz LAN. Puerto 8080 no expuesto a Internet.
- Sin redirección de puertos en router.
- Si el servicio sale de la LAN → TLS y autenticación obligatorios.

## Licencia

MIT
