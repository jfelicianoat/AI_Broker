# AI Broker — Consensus Gateway

[![CI](https://github.com/jfelicianoat/AI_Broker/actions/workflows/ci.yml/badge.svg)](https://github.com/jfelicianoat/AI_Broker/actions/workflows/ci.yml)

Gateway inteligente de inferencia multi-LLM con ejecución por consenso (*mixture of agents*), planificación adaptativa de recursos, cola durable y trazabilidad completa vía event sourcing.

Cada push a `main` ejecuta el CI (GitHub Actions) en Windows y Ubuntu: Ruff, Mypy y la suite completa (183 tests) con cobertura mínima exigida del 90%, instalando desde `requirements.lock` en un entorno limpio.

Estado actual: fases 1–4 operativas, base 5.0 completada y panel operativo 5.2 disponible sobre los read models de 5.1. El Broker usa proveedores reales, descubre el catálogo de Ollama, ejecuta chat o embeddings, admite destino exacto y aplica timeout global. `fast` es serial; `slow` puede lanzar proponentes concurrentes dentro de un solo workflow. El proveedor `bootstrap` queda reservado para pruebas.

La selección de modelos es **adaptativa**: los candidatos que pasan los filtros (proveedor, privacidad cloud/local, capacidad, contexto) se reordenan por un score multiobjetivo — fiabilidad histórica (tasa de éxito con suavizado de Laplace), latencia y coste medios — calculado sobre la evidencia real de `model_invocations` en una ventana configurable (`routing:` en `broker_config.yaml`). Un modelo sin historial puntúa neutro (el arranque en frío no castiga) y `target_model`/`preferred_model` mantienen prioridad absoluta sobre el score.

## Stack

| Capa | Tecnología |
|------|------------|
| Framework | FastAPI (Python 3.10+) |
| Validación | Pydantic v2 (schemas estrictos, `extra="forbid"`) |
| Persistencia | SQLite + WAL + event sourcing |
| Serialización | JSON canónico con `separators=(',',':')` |
| Scheduling | Un workflow activo; `fast` serial y `slow` con paralelismo interno acotado |
| LLMs | Ollama (local), Hugging Face local, DeepSeek (cloud), extensible |
| Panel | Jinja2 + fragmentos hipermedia y recursos locales |

## Estado de fase 5.3

La base del Probador de Prompts esta implementada en `GET /dashboard/prompt-tester` y `POST /dashboard/actions/prompt-tester`. Permite validar y encolar pruebas `single` con modelo exacto o `mixture_of_agents/fast|slow` con seleccion manual de proponentes, roles y arbitro. Usa `TaskCreateRequest`, `TaskRepository` y la cola durable normal; no llama directamente a providers.

Pendiente del probador: historial HTML filtrado por `origin = prompt_tester`, cancelacion/repeticion desde la propia pantalla, resultado raw, metricas, fallback y metadata de consenso.

## Estado de fase 5.4

La base del Comparador esta implementada en `GET /dashboard/comparison`. Lista tareas `mixture_of_agents`, muestra proponentes y arbitro persistidos, uso/coste/latencia, plan solicitado/efectivo y carriles temporales basados en `model_invocations.started_at/completed_at`. Solo marca solapamiento cuando los timestamps persistidos lo demuestran; no muestra confianza, atribucion ni paralelismo simulado.

Pendiente del comparador: filtros/paginacion completos, detalles por candidato, comparacion entre varias tareas, visual QA en navegador y endurecimiento de seguridad 5.5.

## Estado de fase 5.5

La base de seguridad del dashboard esta implementada: las acciones mutables bajo `/dashboard/actions/*` validan token CSRF de doble envio y `Origin`/`Referer` frente a `Host`. Las paginas HTML sirven token en meta/formulario y el runtime local lo envia como `X-CSRF-Token`. La app añade cabeceras `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options` y `Referrer-Policy`.

Autenticacion administrativa: si existe un token (variable `AI_BROKER_ADMIN_TOKEN` o keyring `ai-broker/dashboard_admin_token`), las acciones mutables exigen sesion admin. El operador inicia sesion en `GET /dashboard/login` (cookie HttpOnly con hash del token) o envia el token en la cabecera `X-Admin-Token`. Sin token configurado, el panel queda abierto (modo LAN privada original).

```powershell
python -c "import getpass,keyring; keyring.set_password('ai-broker','dashboard_admin_token',getpass.getpass('Admin token: '))"
```

Pendiente de 5.5: CSP sin `unsafe-inline` cuando los carriles no dependan de estilos inline, QA visual automatizada y auditoria persistente de acciones administrativas.

## Estado de fase 6

Primer bloque operativo implementado: backup, verificacion y restore del estado durable. El backup genera un zip atomico con snapshot SQLite consistente, artefactos de tareas y manifest con SHA-256 por archivo. La restauracion verifica el backup antes de escribir y exige `--replace` si va a sobrescribir una base o artefactos existentes.

Segundo bloque operativo implementado: logging JSON Lines con rotacion por tamaño. El access log registra metodo, ruta, estado, duracion y cliente; no registra cuerpos, prompts ni respuestas. La configuracion vive en `logging` dentro de `broker_config.yaml`.

Tercer bloque operativo implementado: runner de produccion, scripts de servicio Windows mediante NSSM, comprobador de readiness y script de firewall LAN privado. Los scripts no se ejecutan automaticamente; quedan preparados para despliegue manual.

## Funcionalidades

### 1. Consenso multi-LLM (*mixture of agents*)

- **Estrategia `single`**: inferencia con un solo modelo (modo legacy)
- **Estrategia `mixture_of_agents/fast`**: consenso técnico con múltiples proponentes y un árbitro, ejecutados serialmente
- **Estrategia `mixture_of_agents/slow`**: proponentes paralelos o por oleadas y árbitro posterior
- **Presets implementados**: `fast` y base funcional de `slow`; su activación productiva requiere todavía smoke tests con providers reales y telemetría completa de recursos
- Pipeline implementado: `resource_planning → proposing → synthesizing`
- **Roles con system prompt real**: cada proponente recibe un system prompt según su rol (`generalist`, `specialist`, `skeptic`, `analyst`, `reviewer`) y el árbitro sintetiza con la petición original y los candidatos delimitados (`<original_request>`, `<candidate_N>`), tratándolos como datos y no como instrucciones. La estrategia `single` sigue siendo transparente, sin system prompt
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
- Hugging Face local opcional con modelos descargados en disco y cargados con `transformers`
- DeepSeek opcional con credencial en variable de entorno o Windows Credential Manager
- Analisis de compatibilidad por tandas, sin repetir modelos ya comprobados cuando `probe_skip_checked` esta activo
- Control preventivo y acumulado de `max_cost_usd`
- Selección exacta opcional mediante `target_model.provider/deployment/model`
- Timeout efectivo de tarea con cancelación de las operaciones provider pendientes

### 6. Configuración declarativa

- Archivo YAML (`broker_config.yaml`) con merge profundo sobre defaults
- Secciones: `server`, `persistence`, `processing`, `prompt_compression`, `resources`, `health`, `logging`, `providers`
- Validación en arranque via Pydantic

Para modelos locales se usa LM Studio (proveedor `custom` OpenAI-compatible): descarga modelos de Hugging Face en formato GGUF cuantizado y los expone en `http://127.0.0.1:1234/v1`. El antiguo proveedor `huggingface_local` (ejecución in-process con transformers) se retiró en julio 2026 por redundante; está en el historial de git si hiciera falta recuperarlo.

Ejemplo minimo para LM Studio local:

```yaml
providers:
  custom:
    - id: lmstudio
      enabled: true
      adapter: openai_compatible
      display_name: LM Studio
      base_url: http://127.0.0.1:1234/v1
      api_key_env: null
      deployment: local
      auto_start: true
      sync_models: true
      default_context_window: 32768
      models: []
```

LM Studio debe tener activo su servidor local OpenAI-compatible. Con `auto_start: true`, el Broker ejecuta `lms server status` al arrancar y, si el servidor no esta corriendo, lanza `lms server start --port <puerto de base_url>`. Con `sync_models: true`, el Broker lee el catalogo desde `/v1/models`; si prefieres fijar ventanas de contexto por modelo, puedes desactivar `sync_models` y declarar `models` manualmente.

### 7. Health checks

- Endpoints: `/health` (detallado), `/health/live` (liveness), `/health/ready` (readiness)
- Dependencias con estado `healthy` / `degraded` / `unavailable`
- Latencia medida en ms por dependencia
- SQLite y proveedores configurados; Ollama caído degrada el servicio sin bloquear la cola

### 8. Inferencia transparente

- `inference_kind`: `chat` por defecto o `embedding` local con estrategia `single`
- Preflight conservador de contexto; nunca trunca, divide o sintetiza silenciosamente
- Traducción lossless del prompt/input al proveedor; con `prompt_compression` activo, el prompt de chat se comprime antes del envío (los embeddings nunca se comprimen y el original persiste intacto)
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

### 11. Compresión de prompts

- Servicio determinista por reglas que reduce los tokens del prompt antes de enviarlo a los LLMs, adaptando a español las técnicas de [caveman](https://github.com/JuliusBrussee/caveman), [caveman-micro](https://github.com/kuba-guzik/caveman-micro) y [ponytail](https://github.com/DietrichGebert/ponytail)
- Tres niveles: `light` (cortesías y aperturas sociales), `medium` (además muletillas, relleno y envoltorios de petición) y `aggressive` (además artículos, estilo caveman)
- El código (fences e inline), las URLs y los correos se preservan byte a byte; los embeddings nunca se comprimen; si la compresión dejara menos del 20% del texto, se envía el original
- El prompt original persiste intacto en la base de datos y los artefactos; solo se comprime lo que viaja al proveedor
- Desactivable desde el panel de configuración del dashboard o desde `broker_config.yaml`; los cambios aplican en caliente
- Override por tarea: el campo opcional `prompt_compression` del contrato (`off`/`light`/`medium`/`aggressive`) fija la compresión de esa tarea; el probador lo expone como selector
- Cada compresión efectiva se registra en el log (`prompt.compressed` con caracteres antes/después y ratio) y como evento persistente de la tarea: el detalle muestra el prompt original y el comprimido que viajó

```yaml
prompt_compression:
  enabled: true        # false para desactivar el servicio
  level: medium        # light | medium | aggressive
  min_chars: 40        # prompts más cortos se envían tal cual
```

Especificación completa: [`docs/Prompt_Compression.md`](docs/Prompt_Compression.md).

### 12. Enriquecimiento del catálogo (models.dev)

- Con `model_enrichment.enabled: true`, el broker descarga a diario el catálogo gratuito de [models.dev](https://models.dev) (sin clave; caché en disco, sin dependencia de internet para arrancar)
- Aporta por modelo casado: contexto real, capacidades declaradas (visión/JSON/tools), corte de conocimiento, fecha de publicación y precios de referencia por 1M (solo si el casado es con el proveedor equivalente)
- Jerarquía de evidencia: sondeo real > declarado por el runtime > catálogo externo > heurística por nombre — el catálogo rellena huecos, nunca pisa un dato verificado
- En la página Modelos: columna "Precio 1M", contexto enriquecido y capacidades de catálogo en la columna Funciones (marcadas "catálogo:") y en los chips de filtro

```yaml
model_enrichment:
  enabled: true        # opt-in; url y refresh_hours configurables
```

### 13. Estrategia agent (skills técnicas)

- Estrategia de ejecución `agent` (junto a `single` y `mixture_of_agents`): el modelo usa herramientas en un bucle de tool-calling hasta responder
- Skills genéricas y neutrales al dominio: `web_search` (DuckDuckGo, sin clave), `fetch_url` (lee una URL pública como texto, con guardia SSRF que rechaza hosts privados/loopback), `calculator` (aritmética exacta con AST restringido) y `current_datetime` (fecha/hora UTC y local)
- Guardarraíles: `max_iterations` (1-20) y el presupuesto `max_cost_usd` cortan el loop; requiere un modelo con tools verificado
- Cada llamada a skill se persiste como evento `agent.tool_call` y se muestra en el detalle de tarea (panel "Actividad del agente"); disponible en el probador como estrategia "Agente con skills"
- Los resultados de skill son datos externos no confiables: el system prompt del agente ignora instrucciones embebidas, como el sandboxing del árbitro en el consenso
- Proponentes del mixture con skills (`execution.proposer_skills`): cada proponente puede verificar datos con herramientas antes de proponer (opt-in, el árbitro no usa tools); en el probador es la casilla "Dar herramientas a los proponentes"
- Passthrough de tools del cliente (`execution.agent.client_tools`): el cliente declara sus tools de dominio; al llamarlas, la tarea pasa a `waiting_for_tools` con las llamadas pendientes, el cliente las resuelve con `POST /tasks/{id}/tool_results` y el broker reanuda el bucle. El broker nunca ejecuta tools del cliente: mantiene la neutralidad de dominio

### 14. Meta-router de estrategia (`strategy: auto`)

- Con `strategy_router.enabled`, una tarea `strategy: auto` deja que el broker elija entre `single`, `agent` o `mixture_of_agents`
- Clasificación técnica (no de dominio): datos actuales/cálculo/URL → agente; deliberativa y con presupuesto → mixture; directa → modelo único
- Cada decisión se registra como evento `strategy.routed` (señales + motivos), visible en el detalle de tarea y como caso para el aprendizaje futuro
- Tres piezas activables por separado en config: (1) clasificador heurístico, (2) escalado por confianza y (3) aprendizaje adaptativo; `record_cases` alimenta el aprendizaje desde el principio
- Escalado por confianza: un modelo juez puntúa la respuesta `single`; si baja de `escalation_min_confidence`, la tarea escala a mixture (eventos `strategy.confidence` y `strategy.escalated`)
- Aprendizaje adaptativo: cada tarea auto-enrutada guarda un caso en `routing_cases`; con suficiente evidencia por tipo de petición, el router afina su decisión (p. ej. si el `single` suele escalar, va directo a mixture y ahorra el intento). Marcado `learned: true` en el evento de enrutamiento

```yaml
strategy_router:
  enabled: true          # habilita strategy: auto
  heuristic_classifier: true   # pieza 1
  record_cases: true           # guarda casos para la pieza 3
```

## API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/v1/tasks` | POST | Crear tarea (`202`) o recuperar la misma operación idempotente (`200`) |
| `/api/v1/tasks/{id}` | GET | Estado y resultado de tarea |
| `/api/v1/tasks/{id}` | DELETE | Cancelación idempotente |
| `/api/v1/tasks/{id}/tool_results` | POST | Passthrough: entregar resultados de tools del cliente y reanudar la tarea |
| `/api/v1/queue` | GET | Snapshot de cola (pending / active / terminal) |
| `/api/v1/queue` | PATCH | Reordenar tareas pendientes |
| `/api/v1/dispatcher/tick` | POST | Tick manual de diagnóstico; el dispatcher normal es autónomo |
| `/api/v1/models` | GET | Modelos disponibles, con compatibilidad y capacidades sondeadas (`features`) |
| `/api/v1/models/availability` | GET | Disponibilidad operativa por modelo (operativo / no operativo / error temporal / pendiente) |
| `/api/v1/models/context` | GET | Contexto y matriz de capacidades de un modelo; el sondeo real prevalece sobre la inferencia por nombre |
| `/api/v1/capabilities` | GET | Versión de contrato (2.3), estrategias, presets, `prompt_compression_override` y `agent_skills` |
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
- Modo `local_only`: conserva solo proveedores locales (`ollama` y `lmstudio`) y deshabilita cloud automáticamente

### Recuperación

- Al arrancar: tareas en estado activo se devuelven a `queued` con `attempt + 1`
- Límite de reintentos: al superar `processing.max_task_attempts` (3 por defecto), la tarea interrumpida pasa a `failed` con `TASK_RETRY_LIMIT_EXCEEDED` en lugar de re-encolarse indefinidamente
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
python -m venv .venv
.venv\Scripts\pip install -r requirements.lock   # versiones exactas verificadas
.venv\Scripts\pip install -e . --no-deps
.venv\Scripts\python scripts\run_broker.py --config broker_config.yaml
```

El entorno es reproducible en cualquier PC: `requirements.lock` congela las
versiones exactas (directas y transitivas) con las que pasa la suite. El venv
no se puede copiar entre máquinas (guarda rutas absolutas); recréalo siempre
con los comandos de arriba. Para actualizar dependencias: `pip install -e
.[dev]`, pasar los tests y regenerar el lock con
`pip freeze --exclude-editable > requirements.lock`.

Verificación local:

```bash
.venv\Scripts\python -m pytest --cov=app --cov-report=term-missing
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy
```

Para desarrollo con autoreload (la app se construye con la factory; no existe
una instancia global `app.main:app`):

```bash
uvicorn app.main:create_app --factory --reload --port 8765
```

Abre `http://127.0.0.1:8765/dashboard` para usar el panel operativo. Para una previsualización aislada con SQLite temporal y provider de prueba:

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

Servicio Windows y readiness:

```powershell
python scripts/run_broker.py --config broker_config.yaml
.\scripts\install_windows_service.ps1 -ServiceName "AI-Broker" -ProjectRoot "D:\Desarrollo\Proyectos TFM\AI_Broker"
Start-Service "AI-Broker"
python scripts/check_readiness.py --url http://127.0.0.1:8765/health/ready --timeout 60
.\scripts\configure_firewall_lan.ps1 -Port 8765 -WhatIf
```

## Estructura del proyecto

```
├── app/
│   ├── artifacts.py        # Almacén atómico con hash SHA-256
│   ├── config.py           # Configuración YAML + merge profundo
│   ├── coordinator.py      # Orquestador de consenso multi-LLM
│   ├── db.py               # SQLite con WAL, schema y event sourcing
│   ├── dashboard.py        # Read models paginados y métricas operativas
│   ├── dashboard_filters.py # Filtros Jinja2 del panel
│   ├── dashboard_web.py    # Rutas HTML, fragmentos y acciones del panel
│   ├── main.py             # FastAPI app + endpoints
│   ├── prompt_compressor.py # Compresión de prompts antes de la inferencia
│   ├── providers/          # Paquete de proveedores: base, ollama, deepseek,
│   │                       # openai_compatible, routing, bootstrap
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
│   ├── test_prompt_compressor.py # Tests del servicio de compresión de prompts
│   ├── test_providers.py   # Tests de proveedores, routing, VRAM y presupuesto
│   └── test_phase_four_inference.py  # Contexto, embeddings y resultados opacos
├── broker_config.yaml      # Configuración del broker
├── pyproject.toml          # Proyecto Python + dependencias
└── state/tasks/            # Artefactos de ejecución (gitignored)
```

## Licencia

MIT
