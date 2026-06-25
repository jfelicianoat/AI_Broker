# AI Broker — Consensus Gateway

Gateway inteligente de inferencia multi-LLM con ejecución por consenso (*mixture of agents*), planificación adaptativa de recursos, cola durable y trazabilidad completa vía event sourcing.

Estado actual: fases 1–4 operativas, base 5.0 completada y panel operativo 5.2 disponible sobre los read models de 5.1. El Broker usa proveedores reales, descubre el catálogo de Ollama, ejecuta chat o embeddings, admite destino exacto y aplica timeout global. `fast` es serial; `slow` puede lanzar proponentes concurrentes dentro de un solo workflow. El proveedor `bootstrap` queda reservado para pruebas.

## Stack

| Capa | Tecnología |
|------|------------|
| Framework | FastAPI (Python 3.10+) |
| Validación | Pydantic v2 (schemas estrictos, `extra="forbid"`) |
| Persistencia | SQLite + WAL + event sourcing |
| Serialización | JSON canónico con `separators=(',',':')` |
| Scheduling | Un workflow activo; `fast` serial y `slow` con paralelismo interno acotado |
| LLMs | Ollama (local), DeepSeek (cloud), extensible |
| Panel | Jinja2 + fragmentos hipermedia y recursos locales |

## Estado de fase 5.3

La base del Probador de Prompts esta implementada en `GET /dashboard/prompt-tester` y `POST /dashboard/actions/prompt-tester`. Permite validar y encolar pruebas `single` con modelo exacto o `mixture_of_agents/fast|slow` con seleccion manual de proponentes, roles y arbitro. Usa `TaskCreateRequest`, `TaskRepository` y la cola durable normal; no llama directamente a providers.

Pendiente del probador: historial HTML filtrado por `origin = prompt_tester`, cancelacion/repeticion desde la propia pantalla, resultado raw, metricas, fallback y metadata de consenso.

## Estado de fase 5.4

La base del Comparador esta implementada en `GET /dashboard/comparison`. Lista tareas `mixture_of_agents`, muestra proponentes y arbitro persistidos, uso/coste/latencia, plan solicitado/efectivo y carriles temporales basados en `model_invocations.started_at/completed_at`. Solo marca solapamiento cuando los timestamps persistidos lo demuestran; no muestra confianza, atribucion ni paralelismo simulado.

Pendiente del comparador: filtros/paginacion completos, detalles por candidato, comparacion entre varias tareas, visual QA en navegador y endurecimiento de seguridad 5.5.

## Estado de fase 5.5

La base de seguridad del dashboard esta implementada: las acciones mutables bajo `/dashboard/actions/*` validan token CSRF de doble envio y `Origin`/`Referer` frente a `Host`. Las paginas HTML sirven token en meta/formulario y el runtime local lo envia como `X-CSRF-Token`. La app añade cabeceras `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options` y `Referrer-Policy`.

Pendiente de 5.5: autenticacion administrativa real, CSP sin `unsafe-inline` cuando los carriles no dependan de estilos inline, QA visual automatizada y auditoria persistente de acciones administrativas.

## Estado de fase 6

Primer bloque operativo implementado: backup, verificacion y restore del estado durable. El backup genera un zip atomico con snapshot SQLite consistente, artefactos de tareas y manifest con SHA-256 por archivo. La restauracion verifica el backup antes de escribir y exige `--replace` si va a sobrescribir una base o artefactos existentes.

## Funcionalidades

### 1. Consenso multi-LLM (*mixture of agents*)

- **Estrategia `single`**: inferencia con un solo modelo (modo legacy)
- **Estrategia `mixture_of_agents/fast`**: consenso técnico con múltiples proponentes y un árbitro, ejecutados serialmente
- **Estrategia `mixture_of_agents/slow`**: proponentes paralelos o por oleadas y árbitro posterior
- **Presets implementados**: `fast` y base funcional de `slow`; su activación productiva requiere todavía smoke tests con providers reales y telemetría completa de recursos
- Pipeline implementado: `resource_planning → proposing → synthesizing`
- Trazabilidad total: cada invocación individual queda registrada con tokens, coste y latencia

### 2. Planificación de recursos

- **Modo efectivo actual**: `sequential`; `slow` habilitará `parallel`, `waves` o `sequential` según recursos
- Cálculo preventivo de capacidad según VRAM disponible (`local_vram_budget_gb`)
- Nunca se activan dos workflows. `single/fast` usan una invocación; `slow` podrá admitir varias invocaciones de proponentes dentro de ese workflow
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
- Selección exacta opcional mediante `target_model.provider/deployment/model`
- Timeout efectivo de tarea con cancelación de las operaciones provider pendientes

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

### 9. Probador de Prompts — fase 5.3 base implementada

- Entrada como prompt libre o JSON con validación sintáctica
- Ejecución contra un modelo exacto o un `mixture_of_agents/fast|slow` determinado manualmente cuando cada preset esté operativo
- Selección explícita de proponentes, roles y árbitro desde el catálogo real
- Controles de temperatura, tokens, formato/schema, privacidad, cloud, fallback, timeout y coste
- Uso obligatorio de la misma API y cola durable; la UI no llama directamente a providers
- Resultado raw, métricas, modelo efectivo, fallback y metadata de consenso
- Historial persistente, cancelación y repetición segura; prompts y respuestas siempre escapados
- Vista comparativa con línea temporal serial para `fast` y carriles concurrentes medidos para `slow`, sin métricas simuladas

Especificaciones: [`docs/Prompt_Tester.md`](docs/Prompt_Tester.md), [`docs/Phase_5_Dashboard.md`](docs/Phase_5_Dashboard.md) y [`docs/Mixture_Slow_Concurrency.md`](docs/Mixture_Slow_Concurrency.md).

### 10. Panel operativo — fase 5.2

- Disponible en `GET /dashboard`, sin CDN ni métricas simuladas.
- Resume cola, tarea activa, latencia, coste real, éxito, salud y VRAM observada.
- Refresca cada bloque de forma independiente mientras una inferencia está esperando al LLM.
- Permite subir, bajar y cancelar tareas mediante las mismas operaciones durables del Broker.
- Las cifras proceden de SQLite, del health check actual o del snapshot de recursos con timestamp.
- Un fallo del snapshot de Ollama degrada el bloque de recursos a `N/D` sin inutilizar el panel.
- El historial proactivo de salud y la medición temporal de solapamiento de `slow` siguen pendientes.

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
| `/api/v1/capabilities` | GET | Versión de contrato, presets, scheduling y límites admitidos |
| `/api/v1/usage` | GET | Uso mensual por proveedor |
| `/api/v1/dashboard/summary` | GET | Resumen operativo por ventana temporal |
| `/api/v1/dashboard/tasks` | GET | Tareas paginadas y filtrables |
| `/api/v1/dashboard/tasks/{id}` | GET | Request, resultado, invocaciones y eventos de una tarea |
| `/api/v1/dashboard/resources` | GET | Snapshot de VRAM, reservas, leases y modelos cargados |
| `/dashboard` | GET | Panel operativo local |
| `/dashboard/prompt-tester` | GET | Probador de Prompts |
| `/dashboard/comparison` | GET | Comparador de tareas `mixture_of_agents` |
| `/dashboard/fragments/*` | GET | Fragmentos de resumen, cola, tarea activa, salud y recursos |
| `/dashboard/actions/prompt-tester` | POST | Validar o encolar una prueba de prompt |
| `/dashboard/actions/queue/{id}/{direction}` | POST | Subir o bajar una tarea pendiente |
| `/dashboard/actions/tasks/{id}/cancel` | POST | Solicitar cancelación desde el panel |
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

El Broker recibe inferencias ya preparadas. Su responsabilidad termina en validar el contrato técnico, encolar, enrutar y devolver la respuesta. Cuando se solicita explícitamente `mixture_of_agents/fast`, aplica un algoritmo técnico versionado de proponentes y árbitro; no crea pasos de negocio, no resuelve placeholders, no divide contenido y no interpreta respuestas.

### API y persistencia

- Validación Pydantic estricta con `extra="forbid"`. Petición inválida → `422 CONTRACT_VALIDATION_FAILED`
- Persistencia en SQLite WAL (`state/broker.db`)
- Event sourcing completo: toda mutación registra un evento en la tabla `events`
- Reordenación validada: requiere la lista completa de IDs pendientes

### Ejecución por consenso

- Un solo workflow activo global (`max_active_workflows: 1`)
- Un único workflow activo globalmente; `single/fast` ejecutan una llamada cada vez y `slow` permite paralelismo únicamente entre sus proponentes, dentro de límites reservados
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

### Estrategia `mixture_of_agents` / preset `slow`
```
queued → resource_planning → proposing (parallel/waves/sequential) → synthesizing → completed
```

### Estados reservados para `standard`, `verified`, `high_stakes` — no implementados
```
queued → resource_planning → proposing → evaluating → synthesizing → completed
```

Cada tarea puede terminal en `completed`, `failed` o `cancelled` desde cualquier estado.

## Inicio rápido

```bash
pip install -e .[dev]
uvicorn app.main:app --reload --port 8080 --workers 1
```

Abre `http://127.0.0.1:8080/dashboard` para usar el panel operativo. Para una previsualización aislada con SQLite temporal y provider de prueba:

```powershell
python scripts/preview_dashboard.py --port 8765 --database "$env:TEMP\ai-broker-preview.db"
```

Para activar DeepSeek, configura sus precios en `broker_config.yaml`, cambia `providers.deepseek.enabled` a `true` y guarda la clave sin mostrarla en consola:

```powershell
python -c "import getpass,keyring; keyring.set_password('ai-broker','deepseek_api_key',getpass.getpass('DeepSeek API key: '))"
```

Backup operativo:

```powershell
python scripts/backup_state.py backup --database state/broker.db --artifacts state/tasks --output backups/ai-broker-state.zip
python scripts/backup_state.py verify --backup backups/ai-broker-state.zip
python scripts/backup_state.py restore --backup backups/ai-broker-state.zip --database state/broker.db --artifacts state/tasks --replace
```

## Estructura del proyecto

```
├── app/
│   ├── artifacts.py        # Almacén atómico con hash SHA-256
│   ├── config.py           # Configuración YAML + merge profundo
│   ├── coordinator.py      # Orquestador de consenso multi-LLM
│   ├── db.py               # SQLite con WAL, schema y event sourcing
│   ├── dashboard.py        # Read models paginados y métricas operativas
│   ├── dashboard_web.py    # Rutas HTML, fragmentos y acciones del panel
│   ├── main.py             # FastAPI app + endpoints
│   ├── providers.py        # Ollama, DeepSeek, routing, secretos y ciclo de VRAM
│   ├── repository.py       # Capa de acceso a datos
│   ├── resource_scheduler.py  # Planificador adaptativo de recursos
│   ├── schemas.py          # Modelos Pydantic (contrato completo)
│   ├── static/             # CSS y runtime hipermedia locales
│   └── templates/          # Plantillas Jinja2 del panel
├── scripts/
│   └── preview_dashboard.py # Servidor de previsualización local
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
