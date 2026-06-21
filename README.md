# 🧠 AI Broker — Neural Gateway Service

Gateway inteligente entre un pipeline de captura de YouTube y procesamiento con LLMs locales (Ollama) y externos (DeepSeek). Enrutamiento dinámico, cola serial, dashboard HTMX y control de presupuesto.

## Arquitectura

```
YouTuber ──► Plugin Chrome ──► Orchestrator ──► AI Broker ──► Ollama / DeepSeek
                                                    │
                                              Dashboard Web
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

## Despliegue rápido

```bash
# Broker (máquina con GPU)
python -m venv venv && venv\Scripts\activate
pip install fastapi uvicorn httpx pyyaml jinja2 python-multipart structlog psutil python-dotenv
uvicorn app.main:app --host 192.168.1.x --port 8080 --workers 1

# Orchestrator (máquina principal)
pip install customtkinter watchdog httpx pyyaml markdown matplotlib
python -m orchestrator.main
```

Ver [Deployment_Guide.md](Deployment_Guide.md) para la instalación completa.

## Contrato normativo del MVP

- API asíncrona con persistencia SQLite (`state/broker.db`)
- Planificador estrictamente serial: `max_active_llm_tasks: 1`
- Enrutamiento respeta `preferred_model`, `fallback_allowed`, `max_cost_usd` y presupuesto mensual
- Chunking por límites naturales con solape, sin truncar silenciosamente
- Cancelación con descarga de modelo (`keep_alive: 0`) solo si ninguna otra tarea lo usa
- Sin autenticación entre máquinas (decisión del MVP). CORS desactivado. Solo LAN privada.
- API keys solo en `.env`, visibles solo últimos 4 caracteres en dashboard

## Seguridad operativa (MVP)

- Escuchar solo en interfaz LAN. Puerto 8080 no expuesto a Internet.
- Sin redirección de puertos en router.
- Si el servicio sale de la LAN → TLS y autenticación obligatorios.

## Licencia

MIT
