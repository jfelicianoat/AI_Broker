# AI Broker — Consensus Gateway

Gateway inteligente de inferencia multi-LLM con ejecución por consenso (*mixture of agents*), planificación adaptativa de recursos, cola durable y trazabilidad completa vía event sourcing.

Estado actual: fases 1–4 operativas. El Broker usa proveedores reales, descubre el catálogo de Ollama, ejecuta chat o embeddings y mantiene una única llamada LLM activa global. El proveedor `bootstrap` queda reservado para pruebas.

## Stack

| Capa | Tecnología |
|------|------------|
| Framework | FastAPI (Python 3.10+) |
| Validación | Pydantic v2 (schemas estrictos, `extra="forbid"`) |
| Persistencia | SQLite + WAL + event sourcing |
| Serialización | JSON canónico con `separators=(',',':')` |
| Scheduling adaptativo | Paralelo / Waves / Secuencial según VRAM |
| LLMs | Ollama (local), DeepSeek (cloud), extensible |

## Funcionalidades

### 1. Consenso multi-LLM (*mixture of agents*)

- **Estrategia `single`**: inferencia con un solo modelo (modo legacy)
- **Estrategia `mixture_of_agents`**: ejecución por consenso con múltiples proponentes y un árbitro
- **Presets**: `fast` (sin evaluación), `standard`, `verified`, `high_stakes`
- Pipeline por etapas: `resource_planning → proposing → [evaluating] → synthesizing`
- Trazabilidad total: cada invocación individual queda registrada con tokens, coste y latencia

### 2. Planificación adaptativa de recursos

- **Modos de planificación**: `parallel`, `waves`, `sequential`
- Cálculo de capacidad según VRAM disponible (`local_vram_budget_gb`)
- Las invocaciones planificadas se serializan mediante un semáforo global en el MVP
- Reserva de VRAM por tarea y modelo

### 3. Cola durable con event sourcing

- Aceptación asíncrona (`202 Accepted`) con persistencia inmediata
- Cola ordenada por prioridad y posición
- Reordenación mediante PATCH
- Cancelación idempotente con flag `cancel_requested`
- Sistema completo de eventos (`task.created`, `task.status_changed`, `task.cancelled`, `task.recovered`, `queue.reordered`, `model_invocation.completed`, `artifact.created`)
- Recuperación automática al arranque: tareas en estado activo vuelven a `queued` con incremento de `attempt`
- Creación idempotente con `idempotency_key` y hash canónico: replay idéntico devuelve `200`; contenido distinto devuelve `409`
- Dispatcher autónomo de fondo; `/api/v1/dispatcher/tick` queda para diagnóstico y pruebas
- Reclamación atómica `queued → routing` dentro de `BEGIN IMMEDIATE`; el dispatcher automático y el tick manual no pueden activar dos workflows

### 4. Artefactos con integridad criptográfica

- Escritura atómica (temp file + `os.replace`)
- Hash SHA-256 de cada artefacto
- Almacenamiento por tarea en `state/tasks/{task_id}/`
- Trazabilidad en BD: cada artefacto vinculado a su invocación y run de consenso

### 5. Selección de modelos

- **Modos**: `auto`, `manual`, `hybrid`
- Política de diversidad (`different_families`)
- Proponentes requeridos y preferidos
- Árbitro explícito o automático (`strongest_available`)
- Política de sustitución si un modelo no está disponible
- Catálogo real de Ollama (`/api/tags` + `/api/show`) sin listas hardcodeadas
- DeepSeek opcional con credencial en variable de entorno o Windows Credential Manager
- Control preventivo y acumulado de `max_cost_usd`

### 6. Configuración declarativa

- Archivo YAML (`broker_config.yaml`) con merge profundo sobre defaults
- Secciones: `server`, `persistence`, `processing`, `resources`, `health`, `providers`
- Validación en arranque via Pydantic

### 7. Health checks

- Endpoints: `/health` (detallado), `/health/live` (liveness), `/health/ready` (readiness)
- Dependencias con estado `healthy` / `degraded` / `unavailable`
- Latencia medida en ms por dependencia
- SQLite y proveedores configurados; Ollama caído degrada el servicio sin bloquear la cola

### 8. Inferencia transparente

- `inference_kind`: `chat` por defecto o `embedding` local con estrategia `single`
- Preflight conservador de contexto; nunca trunca, divide o sintetiza silenciosamente
- Traducción lossless del prompt/input a Ollama o DeepSeek
- JSON y Markdown permanecen opacos para el Broker
- Resultado con contenido/vector, uso, modelo y fallback
- Invocación y resultado terminal `single` confirmados en una transacción SQLite

### 9. Probador de Prompts — planificado para fase 5

- Entrada como prompt libre o JSON con validación sintáctica
- Ejecución contra un modelo exacto o un `mixture_of_agents/fast` determinado manualmente
- Selección explícita de proponentes, roles y árbitro desde el catálogo real
- Controles de temperatura, tokens, formato/schema, privacidad, cloud, fallback, timeout y coste
- Uso obligatorio de la misma API y cola durable; la UI no llama directamente a providers
- Resultado raw, métricas, modelo efectivo, fallback y metadata de consenso
- Historial persistente, cancelación y repetición segura; prompts y respuestas siempre escapados

Especificación: [`docs/Prompt_Tester.md`](docs/Prompt_Tester.md).

## API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/v1/tasks` | POST | Crear tarea (`202`) o recuperar la misma operación idempotente (`200`) |
| `/api/v1/tasks/{id}` | GET | Estado y resultado de tarea |
| `/api/v1/tasks/{id}` | DELETE | Cancelación idempotente |
| `/api/v1/queue` | GET | Snapshot de cola (pending / active / terminal) |
| `/api/v1/queue` | PATCH | Reordenar tareas pendientes |
| `/api/v1/dispatcher/tick` | POST | Tick manual de diagnóstico; el dispatcher normal es autónomo |
| `/api/v1/models` | GET | Modelos disponibles |
| `/api/v1/usage` | GET | Uso mensual por proveedor |
| `/health` | GET | Estado detallado de dependencias |
| `/health/live` | GET | Liveness del proceso |
| `/health/ready` | GET | Readiness de SQLite y dispatcher |

## Esquema de petición

```json
{
  "idempotency_key": "orchestrator:capture-001:1:single",
  "request_id": "cli-001",
  "inference_kind": "chat",
  "content": {
    "prompt": "Analiza el impacto...",
    "attachments": [],
    "metadata": {}
  },
  "output": {
    "format": "markdown",
    "language": "es"
  },
  "generation": {
    "temperature": 0.3,
    "max_output_tokens": 4000
  },
  "model_requirements": {
    "preferred_model": null,
    "fallback_allowed": true,
    "cloud_allowed": false,
    "allowed_providers": ["ollama"],
    "max_cost_usd": null
  },
  "execution": {
    "strategy": "mixture_of_agents",
    "preset": "fast",
    "scheduling": "adaptive",
    "max_proposers": 3,
    "max_judges": 1,
    "max_rounds": 1,
    "timeout_seconds": 600,
    "early_stop": true,
    "selection": {
      "mode": "auto",
      "diversity_policy": "different_families",
      "arbiter_policy": "strongest_available",
      "proposer_count": 3
    }
  },
  "risk": {
    "data_classification": "internal",
    "human_review_required": false
  },
  "priority": 100
}
```

## Contrato normativo

### Responsabilidad del Broker

El Broker recibe inferencias ya preparadas. Su responsabilidad termina en validar el contrato técnico, encolar, ejecutar la estrategia de consenso (proponentes + árbitro) y devolver la respuesta sintetizada. No genera prompts, no resuelve placeholders, no divide contenido y no interpreta respuestas.

### API y persistencia

- Validación Pydantic estricta con `extra="forbid"`. Petición inválida → `422 CONTRACT_VALIDATION_FAILED`
- Persistencia en SQLite WAL (`state/broker.db`)
- Event sourcing completo: toda mutación registra un evento en la tabla `events`
- Reordenación validada: requiere la lista completa de IDs pendientes

### Ejecución por consenso

- Un solo workflow activo global (`max_active_workflows: 1`)
- Un único workflow y una única llamada LLM activos globalmente; la planificación conserva waves para evolución posterior
- Cada invocación individual se persiste en `model_invocations`
- Si no se alcanza quórum mínimo (2 proponentes), la tarea falla con `CONSENSUS_QUORUM_NOT_REACHED`
- Cancelación en cualquier etapa: verifica `cancel_requested` entre invocaciones

### Seguridad (MVP)

- Sin autenticación entre cliente y broker (solo LAN privada, CORS desactivado por defecto)
- Clasificación de datos: `public`, `internal`, `confidential`, `local_only`
- Modo `local_only`: fuerza proveedor Ollama y deshabilita cloud automáticamente

### Recuperación

- Al arrancar: tareas en estado activo se devuelven a `queued` con `attempt + 1`
- Reconciliación de artefactos huérfanos
- Integridad referencial vía foreign keys con `ON DELETE CASCADE`

## Estados de progreso

### Estrategia `single`
```
queued → generating → completed
```

### Estrategia `mixture_of_agents` / preset `fast`
```
queued → resource_planning → proposing → synthesizing → completed
```

### Estrategia `mixture_of_agents` / preset `standard`, `verified`, `high_stakes`
```
queued → resource_planning → proposing → evaluating → synthesizing → completed
```

Cada tarea puede terminal en `completed`, `failed` o `cancelled` desde cualquier estado.

## Inicio rápido

```bash
pip install -e .[dev]
uvicorn app.main:app --reload --port 8080 --workers 1
```

Para activar DeepSeek, configura sus precios en `broker_config.yaml`, cambia `providers.deepseek.enabled` a `true` y guarda la clave sin mostrarla en consola:

```powershell
python -c "import getpass,keyring; keyring.set_password('ai-broker','deepseek_api_key',getpass.getpass('DeepSeek API key: '))"
```

## Estructura del proyecto

```
├── app/
│   ├── artifacts.py        # Almacén atómico con hash SHA-256
│   ├── config.py           # Configuración YAML + merge profundo
│   ├── coordinator.py      # Orquestador de consenso multi-LLM
│   ├── db.py               # SQLite con WAL, schema y event sourcing
│   ├── main.py             # FastAPI app + endpoints
│   ├── providers.py        # Ollama, DeepSeek, routing, secretos y ciclo de VRAM
│   ├── repository.py       # Capa de acceso a datos
│   ├── resource_scheduler.py  # Planificador adaptativo de recursos
│   └── schemas.py          # Modelos Pydantic (contrato completo)
├── tests/
│   ├── test_api.py         # Tests de integración de API
│   ├── test_contract.py    # Tests de validación de contrato
│   ├── test_providers.py   # Tests de proveedores, routing, VRAM y presupuesto
│   └── test_phase_four_inference.py  # Contexto, embeddings y resultados opacos
├── broker_config.yaml      # Configuración del broker
├── pyproject.toml          # Proyecto Python + dependencias
└── state/tasks/            # Artefactos de ejecución (gitignored)
```

## Licencia

MIT
