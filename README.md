# AI Broker

[![CI](https://github.com/jfelicianoat/AI_Broker/actions/workflows/ci.yml/badge.svg)](https://github.com/jfelicianoat/AI_Broker/actions/workflows/ci.yml)

Tu central privada de inteligencia artificial: un único punto de entrada que recibe peticiones, documentos, imágenes, audio y vídeo, elige los mejores modelos de IA disponibles (locales o en la nube) y devuelve la respuesta — con la posibilidad de que varios modelos deliberen entre sí, de que un agente use herramientas y ejecute código en un entorno aislado, y con trazabilidad completa de todo lo que ocurre.

---

## 1. ¿Qué es el AI Broker?

**Para empezar:** imagina una recepción con un recepcionista muy competente. Tú le entregas un encargo — una pregunta, un informe en PDF, la grabación de una reunión — y él decide a qué especialista dárselo, o si el asunto es delicado, reúne a un comité de especialistas para que cada uno dé su opinión y un moderador redacte la conclusión. Nunca pierde un encargo, apunta todo lo que pasa, y te avisa cuando está listo. Eso es el AI Broker, con modelos de IA como especialistas.

**Un nivel más abajo:** es un servicio que corre en tu propio ordenador (no depende de ninguna nube para funcionar), expone una API y un panel web local, y gestiona una cola de tareas de inferencia. Cada tarea declara qué necesita (privacidad, coste máximo, formato de salida) y el broker decide qué modelo o modelos la ejecutan, entre los que tengas en Ollama, LM Studio o proveedores de API como DeepSeek o NVIDIA.

**Técnicamente:** es un gateway de inferencia multi-LLM construido sobre FastAPI y SQLite, con cola durable (las tareas sobreviven a reinicios), aceptación asíncrona (`202 Accepted` + polling), creación idempotente, *event sourcing* de cada mutación, planificación de VRAM para modelos locales, y un contrato Pydantic estricto (versión 2.4) que las aplicaciones cliente consumen sin acoplarse a ningún proveedor de IA concreto.

## 2. Qué sabe hacer

**En pocas palabras:** le puedes pedir cosas de cuatro maneras — que responda un solo modelo, que deliberen varios, que un agente investigue con herramientas, o que el propio broker elija la mejor manera. Y le puedes adjuntar casi cualquier fichero: lo convierte a texto antes de trabajar con él.

| Capacidad | En una frase |
|---|---|
| Estrategia `single` | Un modelo responde directamente |
| Estrategia `mixture_of_agents` | Varios modelos proponen, un árbitro sintetiza la mejor respuesta |
| Estrategia `agent` | El modelo usa herramientas (buscar en web, leer URLs, calcular, ejecutar código) en bucle hasta resolver |
| Estrategia `auto` | El meta-router clasifica la petición y elige la estrategia por ti |
| Ficheros adjuntos | PDF (incluso escaneados, con OCR), Office, imágenes, audio y vídeo → Markdown |
| Sandbox de código | El agente ejecuta Python real en contenedores Docker desechables y aislados |
| Selección adaptativa | Los modelos se eligen por evidencia real: fiabilidad, latencia y coste históricos |

**Detalle técnico por estrategia:**

- **`single`** — inferencia transparente: el prompt viaja sin interpretación (compresión opcional aparte), el resultado vuelve opaco. Soporta `chat` y `embedding`, formato `markdown`/`text`/`json` (con JSON Schema obligatorio), y destino exacto vía `target_model.provider/deployment/model`.
- **`mixture_of_agents`** (presets `fast`/`slow`) — pipeline `resource_planning → proposing → synthesizing`. Cada proponente recibe un system prompt por rol (`generalist`, `specialist`, `skeptic`, `analyst`, `reviewer`); el árbitro recibe la petición original y los candidatos dentro de delimitadores XML neutralizados (`<original_request>`, `<candidate_N>`) que impiden que un candidato inyecte instrucciones. `fast` es serial; `slow` ejecuta proponentes en `parallel`, `waves` o `sequential` según la VRAM reservable. Quórum mínimo de 2 proponentes o la tarea falla con `CONSENSUS_QUORUM_NOT_REACHED`. Con `proposer_skills`, cada proponente puede usar herramientas antes de proponer (el árbitro nunca).
- **`agent`** — bucle de tool-calling con guardarraíles `max_iterations` (1–20) y `max_cost_usd`. Skills integradas: `web_search` (DuckDuckGo, sin clave), `fetch_url` (con guardia SSRF que resuelve DNS y rechaza hosts no públicos), `calculator` (AST restringido, sin nombres ni llamadas), `current_datetime` y `run_code` (sandbox Docker, opt-in). Passthrough de tools del cliente: la tarea se pausa en `waiting_for_tools`, el cliente resuelve con `POST /tasks/{id}/tool_results` y el bucle se reanuda con la conversación congelada en `agent_state_json`.
- **`auto`** — el meta-router aplica tres piezas activables por separado: clasificador heurístico determinista (señales técnicas: recencia, cálculo, URL, deliberación, longitud), escalado por confianza (un juez puntúa la respuesta single 0–1; por debajo del umbral escala a mixture con el presupuesto restante), y aprendizaje adaptativo (casos persistidos en `routing_cases` agrupados por huella de señales; con evidencia suficiente el router corrige a la heurística). Toda decisión queda como evento `strategy.routed` con señales, motivos y flag `learned`.

## 3. Ficheros adjuntos: de documento a conocimiento

**Para empezar:** puedes darle al broker un PDF de 200 páginas, la foto de un documento, un Excel o la grabación de una reunión. Él lo convierte a texto ordenado, y ese texto acompaña a tu pregunta cuando llega al modelo. Si el PDF es un escaneo, lo "lee" con OCR; si es un vídeo, extrae el audio y lo transcribe; si el documento tiene gráficos, un modelo de visión los describe y la descripción queda insertada en su sitio.

**El flujo:** subes el fichero (`POST /api/v1/files` o la página **Ficheros** del panel) → el broker lo convierte en segundo plano (`received → converting → ready/failed`) → creas la tarea referenciando el `file_id` → al despachar, el Markdown del documento se inyecta en el prompt. Subir dos veces el mismo fichero no repite el trabajo (dedupe por SHA-256), y cada fichero listo muestra sus **tokens estimados** para que elijas modelo con conocimiento de causa.

| Tipo | Formatos | Motor |
|---|---|---|
| PDF (nativo o escaneado) | `.pdf` | Docling, OCR por página (RapidOCR/EasyOCR) |
| Office / eBook / web | `.docx .xlsx .pptx .epub .msg .html .htm .ipynb` | MarkItDown |
| Texto y marcado | `.txt .md .rst .adoc .org .tex .log` | passthrough |
| Código y datos | `.py .js .ts .sql .json .yaml .csv .tsv` y más | passthrough en fence |
| Imagen | `.png .jpg .jpeg .webp .tiff .bmp` | OCR + descripción por LLM de visión |
| Audio | `.mp3 .wav .m4a .flac .ogg .opus .aac` | faster-whisper local |
| Vídeo | `.mp4 .mkv .mov .avi .webm .m4v .wmv` | ffmpeg (extrae audio) + faster-whisper |

**Detalle técnico:** la validación de subida comprueba extensión **y** magic bytes (un `.pdf` que no empieza por `%PDF` se rechaza con `INGEST_CONTENT_MISMATCH`), sanea nombres contra path traversal y aplica límites de tamaño/páginas/timeout. La detección escaneo-vs-nativo es por página, no por documento. Las figuras extraídas de un PDF se envían una a una a un endpoint OpenAI-compatible de visión con el texto adyacente como contexto, y la descripción sustituye al marcador en la posición original (`> **[Figura N — descripción generada por IA]:** …`). En el despacho, el Markdown se inyecta dentro de `<attached_document>` con neutralización de delimitadores (el documento no puede cerrar su propio tag) y una advertencia explícita al modelo de que es contenido no confiable con posibles errores de OCR; el `request_json` persistido conserva el prompt original del cliente. Con adjuntos, la compresión de prompts pasa a `off` salvo override explícito: comprimir tablas o código de un documento los corrompería. Los motores (Docling, MarkItDown, faster-whisper) se importan en perezoso: si falta uno, solo ese fichero falla con `ENGINE_MISSING` y el broker sigue operando. Corpus dorado de regresión en `tests/fixtures/ingestion/`.

## 4. Sandbox: código de la IA sin riesgo para tu máquina

**Para empezar:** los modelos de IA escriben código, y a veces conviene ejecutarlo — para analizar los datos de un Excel adjunto, para comprobar que el código que te van a entregar funciona. Ejecutar código escrito por una IA directamente en tu ordenador sería temerario. El broker lo ejecuta en una "habitación acolchada": un contenedor desechable que no tiene internet, no ve tus ficheros y se destruye al terminar.

**Cómo se usa:** activa la skill "Ejecutar código (sandbox)" en una tarea de agente. El modelo escribe Python, el broker lo ejecuta aislado y le devuelve la salida (o el error, para que lo corrija y reintente). La imagen por defecto (`ai-broker-sandbox:latest`, construible con `docker build -t ai-broker-sandbox:latest sandbox/`) incluye pandas, numpy, matplotlib y openpyxl.

**Detalle técnico:** cada ejecución lanza `docker run --rm` con fronteras **no configurables**: `--network none` (nada que exfiltrar: por el sandbox pasan prompts y documentos potencialmente confidenciales), sin volúmenes del host, `--read-only` con tmpfs efímeros (`/work`, `/tmp`), usuario `nobody` (65534), `--cap-drop ALL`, `no-new-privileges`, y topes de memoria (sin swap extra), CPU y procesos. El código viaja por stdin (sin problemas de quoting en Windows). Timeout en dos capas: `timeout` dentro del contenedor (exit 124 → mensaje claro al modelo) y plazo asyncio externo con margen; si el CLI muere sin limpiar, `docker rm -f` remata por nombre. La skill es doblemente opt-in: no está en las skills por defecto y exige `sandbox.enabled`; sin sandbox, pedirla devuelve `409 SANDBOX_DISABLED`. Docker parado → error de tool legible, la tarea no revienta. Modelo de amenaza honesto: contenedor comparte kernel con la VM de WSL2 — suficiente contra código generado por LLMs y documentos hostiles, no contra 0-days de kernel.

## 5. Cómo elige los modelos

**Para empezar:** no todos los modelos valen para todo, y algunos fallan más que otros. El broker aprende de la experiencia: recuerda qué modelos responden bien, rápido y barato, y prefiere esos. Y antes de usar un modelo comprueba qué sabe hacer de verdad (¿entiende imágenes? ¿puede usar herramientas?), en lugar de fiarse del nombre.

**Las capas de decisión:** primero se filtran los candidatos (proveedores permitidos, política cloud/local, capacidad requerida, ventana de contexto suficiente); después se reordenan por un score multiobjetivo sobre evidencia real — tasa de éxito con suavizado de Laplace, latencia y coste medios normalizados min-max — calculado sobre `model_invocations` en una ventana configurable. Un modelo sin historial puntúa neutro (el arranque en frío no castiga). `target_model`/`preferred_model` mantienen prioridad absoluta.

**Detalle técnico — jerarquía de evidencia sobre capacidades:** sondeo real contra el endpoint (peticiones de 1 token para chat/visión/JSON/tools, persistidas en `features` con timestamp) > capacidades declaradas por el runtime (Ollama `/api/show`) > catálogo externo [models.dev](https://models.dev) (opt-in, descarga diaria cacheada en disco: contexto real, precios por 1M, corte de conocimiento) > heurística por nombre. El catálogo rellena huecos, nunca pisa un dato verificado. La compatibilidad distingue `incompatible` (error de contrato 400/404/422: vetado) de `error` (fallo temporal 5xx/timeout: se reintenta). El preflight de contexto usa una cota superior conservadora de tokens y **nunca** trunca en silencio: si no cabe, recorta `max_output_tokens` o falla con `CONTEXT_LIMIT_EXCEEDED` explicando los números.

## 6. Recursos: una GPU, muchos modelos

**Para empezar:** los modelos locales compiten por la memoria de tu tarjeta gráfica. El broker actúa de árbitro: calcula qué cabe, reserva sitio antes de lanzar nada, y si no caben todos a la vez, los ejecuta por tandas.

**Detalle técnico:** un solo workflow activo global (`max_active_workflows: 1`, validado en config); dentro de un mixture `slow`, el `ResourceScheduler` decide `parallel`/`waves`/`sequential` según la VRAM reservable (`local_vram_budget_gb` − margen de seguridad), con leases de VRAM por modelo y `max_parallel_invocations` (`auto` = fórmula conservadora compartida entre planificador y semáforo de ejecución del router, para que el plan nunca prometa paralelismo que el router no concede). Los proveedores cloud no consumen VRAM pero sí presupuesto (`max_cost_usd` preventivo y acumulado) y cuotas. `unload_after_task` descarga modelos al terminar.

## 7. Compresión de prompts

**Para empezar:** buena parte de un mensaje humano son cortesías y relleno que al modelo no le aportan ("hola, por favor, ¿podrías…?"). El broker puede podarlas antes de enviar, ahorrando tokens (= tiempo y dinero) sin tocar lo importante. Tu texto original nunca se pierde: solo viaja recortado.

**Detalle técnico:** servicio determinista por reglas (adaptación a español de caveman/caveman-micro/ponytail) con tres niveles (`light`/`medium`/`aggressive`); código en fences e inline, URLs y correos se preservan byte a byte; los embeddings nunca se comprimen; si quedara menos del 20% del texto, se envía el original. Override por tarea en el contrato (`prompt_compression: off|light|medium|aggressive`), selector en el probador y vista previa fiel. Cada compresión efectiva persiste como evento `prompt.compressed` (exento de la poda de eventos) con el texto que realmente viajó. Especificación: [`docs/Prompt_Compression.md`](docs/Prompt_Compression.md).

## 8. El panel de control

**Para empezar:** todo lo anterior se maneja desde el navegador en `http://127.0.0.1:8765/dashboard`, sin instalar nada más. Puedes probar prompts, subir ficheros, ver la cola, comparar respuestas de varios modelos y cambiar la configuración — con confirmaciones antes de guardar y sin necesidad de tocar ficheros a mano.

| Página | Qué ofrece |
|---|---|
| **Resumen** | Cola, tarea activa, latencias, coste, salud y VRAM en tiempo real |
| **Tareas** | Cola reordenable, historial, detalle con eventos/invocaciones/prompts |
| **Probador** | Validar y encolar pruebas de cualquier estrategia con adjuntos y skills |
| **Comparación** | Carriles temporales medidos de un mixture (sin paralelismo simulado) |
| **Modelos** | Catálogo con compatibilidad sondeada, capacidades, precios y filtros |
| **Ficheros** | Subir adjuntos, ver estado de conversión, tokens estimados, Markdown |
| **Enrutamiento** | Qué ha aprendido el meta-router por tipo de petición |
| **Configuración** | Límites, compresión, ingesta, sandbox, router y proveedores en caliente |

**Detalle técnico:** Jinja2 + fragmentos hipermedia (HTMX) con recursos 100% locales (sin CDN), auto-refresco por bloque con backoff exponencial, pausa con pestaña oculta y banner de conexión persistente en lugar de tormenta de toasts. Sin métricas simuladas: todo sale de SQLite, del health check o del snapshot de recursos con timestamp. Los cambios de Configuración se escriben al YAML de forma atómica con detección de edición concurrente (huella SHA-256 del fichero en el formulario) y revisión de cambios antes de aplicar; las secciones aplican en caliente (los servicios leen el `BrokerConfig` compartido en vivo).

## 9. Privacidad y seguridad

**Para empezar:** el broker está pensado para que tus datos no salgan de tu ordenador salvo que tú lo permitas expresamente. Puedes marcar una tarea como "solo local" y ningún proveedor de nube la verá jamás. Y todo lo que entra de fuera — documentos, páginas web, respuestas de modelos — se trata como datos, nunca como órdenes.

**Capas de defensa:**

- **Frontera de datos:** `data_classification: local_only` fuerza proveedores locales y desactiva cloud a nivel de contrato (validación, no convención). `cloud_allowed: false` es el default.
- **Anti-inyección sistemática:** candidatos del consenso, resultados de skills y documentos adjuntos viajan en sandboxes XML con delimitadores neutralizados; los system prompts marcan ese contenido como datos no confiables.
- **SSRF:** `fetch_url` resuelve DNS y rechaza cualquier host no público (el dashboard del propio broker incluido).
- **Sandbox:** el código generado por modelos jamás toca el host (sección 4).
- **Panel:** CSRF de doble envío + validación `Origin`/`Referer`, cabeceras CSP/`X-Frame-Options`/`nosniff`, sesión admin con cookie HttpOnly y caducidad deslizante. Con token admin configurado (env `AI_BROKER_ADMIN_TOKEN` o keyring `ai-broker/dashboard_admin_token`), toda lectura con prompts/resultados exige credencial. Arranque fail-closed: el broker se niega a escuchar fuera de loopback sin token (opt-out explícito `allow_unauthenticated_lan`).
- **Credenciales:** claves de API en variables de entorno o Windows Credential Manager (keyring), nunca en el YAML.
- **Logs:** JSON Lines con rotación; el access log no registra cuerpos, prompts ni respuestas.

## 10. Puesta en marcha

**Lo más simple (Windows):** doble clic en `arrancar_ai_broker.bat`, abre `http://127.0.0.1:8765/dashboard`. Para parar, `parar_ai_broker.bat`; para ver el estado, `estado_ai_broker.bat`.

**Instalación desde cero:**

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.lock   # versiones exactas verificadas
.venv\Scripts\pip install -e . --no-deps
.venv\Scripts\python scripts\run_broker.py --config broker_config.yaml
```

**Capacidades opcionales y sus requisitos:**

| Capacidad | Requisito |
|---|---|
| Ingesta de documentos/imágenes/audio | `pip install "ai-broker[ingestion]"` (Docling, MarkItDown, faster-whisper) |
| Transcripción de vídeo | Además, `ffmpeg` (PATH o `ingestion.transcription.ffmpeg_path`) |
| Descripción de figuras | Un endpoint OpenAI-compatible con modelo de visión (p. ej. LM Studio) |
| Sandbox `run_code` | Docker Desktop en marcha + `docker build -t ai-broker-sandbox:latest sandbox/` |
| DeepSeek / NVIDIA cloud | Clave en keyring: `python -c "import getpass,keyring; keyring.set_password('ai-broker','deepseek_api_key',getpass.getpass())"` |

**Detalle técnico:** `requirements.lock` congela versiones directas y transitivas (regenerar con `pip freeze --exclude-editable > requirements.lock` tras pasar la suite). La app se construye con factory (no hay instancia global): desarrollo con `uvicorn app.main:create_app --factory --reload --port 8765`. Verificación local: `pytest --cov=app --cov-report=term-missing` (cobertura mínima 90% en CI, Windows + Ubuntu), `ruff check .`, `mypy`. Servicio Windows vía NSSM (`scripts/install_windows_service.ps1`), readiness con `scripts/check_readiness.py`, firewall LAN con `scripts/configure_firewall_lan.ps1`. Backup/restore atómico con manifest SHA-256: `scripts/backup_state.py backup|verify|restore`. Retenciones configurables en `persistence`: eventos (`events_retention_days`), artefactos (`artifacts_retention_days`) y ficheros ingeridos (`files_retention_days`); `0` = conservar siempre.

## 11. API para desarrolladores

**Para empezar:** cualquier aplicación puede usar el broker con tres llamadas HTTP: crear la tarea, consultar su estado, recoger el resultado. No hace falta SDK.

**El ciclo básico:**

```
POST /api/v1/tasks  →  202 { task_id, status_url }
GET  /api/v1/tasks/{id}  →  { status: queued|…|completed, result }
```

**Petición mínima y petición completa:**

```json
{ "idempotency_key": "app:informe-42", "content": { "prompt": "Resume esto" } }
```

```json
{
  "idempotency_key": "app:informe-42:consenso",
  "inference_kind": "chat",
  "content": {
    "prompt": "Analiza el informe adjunto y extrae riesgos",
    "attachments": [
      { "type": "broker_file", "metadata": { "file_id": "file_abc123" } }
    ]
  },
  "output": { "format": "markdown", "language": "es" },
  "generation": { "temperature": 0.3, "max_output_tokens": 4000 },
  "model_requirements": {
    "cloud_allowed": false,
    "allowed_providers": ["ollama", "lmstudio"],
    "max_cost_usd": 0.5
  },
  "execution": {
    "strategy": "auto",
    "timeout_seconds": 600
  },
  "risk": { "data_classification": "local_only" },
  "prompt_compression": "off",
  "priority": 100
}
```

**Endpoints:**

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/v1/tasks` | POST | Crear tarea (`202`); replay idempotente devuelve `200`; conflicto `409` |
| `/api/v1/tasks/{id}` | GET | Estado, progreso y resultado |
| `/api/v1/tasks/{id}` | DELETE | Cancelación idempotente |
| `/api/v1/tasks/{id}/tool_results` | POST | Resolver tools del cliente y reanudar (`waiting_for_tools`) |
| `/api/v1/files` | POST | Subir fichero adjunto (multipart, `202`, dedupe SHA-256) |
| `/api/v1/files/{id}` | GET | Estado de conversión, metadatos, tokens estimados |
| `/api/v1/files/{id}/markdown` | GET | Markdown resultante (cuando `ready`) |
| `/api/v1/queue` | GET / PATCH | Snapshot de cola / reordenar pendientes |
| `/api/v1/models` | GET | Catálogo con compatibilidad y capacidades sondeadas |
| `/api/v1/models/availability` | GET | Disponibilidad operativa por modelo |
| `/api/v1/models/context` | GET | Contexto y matriz de capacidades de un modelo |
| `/api/v1/capabilities` | GET | Contrato 2.4: estrategias, presets, `file_ingestion`, `ingestion_formats`, `sandbox_run_code`, `agent_skills`, flags del router |
| `/api/v1/usage` | GET | Uso mensual por proveedor |
| `/api/v1/dashboard/*` | GET | Read models: summary, tasks, resources |
| `/api/v1/dispatcher/tick` | POST | Tick manual de diagnóstico (el dispatcher es autónomo) |
| `/health` `/health/live` `/health/ready` | GET | Salud detallada, liveness, readiness |

**Detalle técnico del contrato:** validación Pydantic estricta (`extra="forbid"`; inválido → `422 CONTRACT_VALIDATION_FAILED` con campos). Idempotencia por `idempotency_key` + hash canónico del cuerpo: mismo cuerpo → `200` con la tarea original; cuerpo distinto → `409 IDEMPOTENCY_CONFLICT`. Adjuntos: solo `type: "broker_file"` con `file_id` (por `metadata.file_id` o `uri: broker://files/{id}`); ficheros no listos → `409 ATTACHED_FILE_NOT_READY`. Estados de progreso: `queued → routing/planning/resource_planning → generating|proposing → (evaluating) → synthesizing → completed`, más `waiting_for_tools`, y terminales `completed|failed|cancelled` desde cualquier estado. Recuperación al arranque: tareas activas vuelven a `queued` con `attempt+1` hasta `max_task_attempts` (después `failed` con `TASK_RETRY_LIMIT_EXCEEDED`). Guía de integración completa: [`Agent_AI_Broker.md`](Agent_AI_Broker.md).

## 12. Estructura del proyecto

```
├── app/
│   ├── main.py                # FastAPI app (factory) + endpoints API
│   ├── schemas.py             # Contrato Pydantic completo (v2.4)
│   ├── coordinator.py         # Orquestación: single, mixture, agent, auto
│   ├── strategy_router.py     # Meta-router: clasificador + aprendizaje
│   ├── skills.py              # Skills del agente (web, URL, cálculo, código)
│   ├── sandbox.py             # Ejecutor Docker aislado (skill run_code)
│   ├── ingestion/             # Ficheros adjuntos: detección, motores, servicio
│   ├── providers/             # Ollama, DeepSeek, OpenAI-compatible, routing
│   ├── resource_scheduler.py  # Planificación de VRAM y oleadas
│   ├── model_enrichment.py    # Catálogo externo models.dev
│   ├── prompt_compressor.py   # Compresión de prompts
│   ├── repository.py          # Acceso a datos (tareas, cola, invocaciones)
│   ├── db.py                  # SQLite WAL + esquema + event sourcing
│   ├── dashboard*.py          # Read models, rutas, formularios y filtros del panel
│   ├── artifacts.py           # Artefactos atómicos con SHA-256
│   ├── maintenance.py         # Backup/restore y podas de retención
│   └── templates/ static/     # Panel: Jinja2 + CSS/JS locales
├── sandbox/Dockerfile         # Imagen del sandbox (pandas, numpy, matplotlib)
├── scripts/                   # Runner, servicio Windows, backup, readiness
├── tests/                     # Suite completa + corpus dorado de ingesta
├── docs/                      # Documentación por fases y especificaciones
├── broker_config.yaml         # Configuración declarativa (editable en el panel)
└── state/                     # BD, artefactos y ficheros ingeridos (gitignored)
```

## 13. Documentación

| Documento | Contenido |
|---|---|
| [`Agent_AI_Broker.md`](Agent_AI_Broker.md) | Guía de integración para aplicaciones cliente (contrato normativo) |
| [`Deployment_Guide.md`](Deployment_Guide.md) | Despliegue completo en Windows |
| [`docs/Phase_7_File_Ingestion.md`](docs/Phase_7_File_Ingestion.md) | Ingesta de ficheros adjuntos |
| [`docs/Phase_8_Sandbox.md`](docs/Phase_8_Sandbox.md) | Sandbox de ejecución de código |
| [`docs/Phase_5_Dashboard.md`](docs/Phase_5_Dashboard.md) | Panel operativo (normativo para las pantallas) |
| [`docs/Prompt_Compression.md`](docs/Prompt_Compression.md) | Compresión de prompts |
| [`docs/Prompt_Tester.md`](docs/Prompt_Tester.md) | Probador de prompts |
| [`docs/Mixture_Slow_Concurrency.md`](docs/Mixture_Slow_Concurrency.md) | Concurrencia del preset slow |
| [`docs/Phase_6_Operations.md`](docs/Phase_6_Operations.md) | Operación: backup, logging, servicio |
| [`AI_Broker_consenso_multi_LLM.md`](AI_Broker_consenso_multi_LLM.md) | Diseño original del consenso multi-LLM |

## 14. Licencia

MIT
