# Graph Report - .  (2026-07-09)

## Corpus Check
- 70 files · ~69,690 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 933 nodes · 3111 edges · 64 communities (39 shown, 25 thin omitted)
- Extraction: 81% EXTRACTED · 19% INFERRED · 0% AMBIGUOUS · INFERRED: 589 edges (avg confidence: 0.54)
- Token cost: 382,416 input · 0 output

## Community Hubs (Navigation)
- Configuration Schema
- Provider Base Layer
- Config Loading & Admin Auth
- Admin Auth & Login Throttle
- Ollama Model Research
- App Bootstrap & Health
- Task Repository
- Concurrency & Provider Design
- Consensus Coordinator
- Dashboard Query Repository
- Maintenance & Backup
- Resource Scheduler
- Dashboard Frontend JS
- Routed Model Provider
- Prompt Compressor & Tests
- Database Layer
- Dashboard Template Filters
- Multi-LLM Consensus Design
- Admin Auth API Routes
- Dashboard Templates & Docs
- Inference Contract & Compression Refs
- Agent Broker Contract Sections
- Prompt Compressor Implementation
- Provider Generate/Propose/Synthesize
- Bootstrap Model Provider
- Config & Resources Templates
- Prompt Tester & Dashboard Docs
- Single Workflow Constraint
- Artifact Storage
- Logging Configuration
- Model Comparison Templates
- Model Selection & Routing
- Prompt Compression Config
- Artifact Persistence Schema
- DeepSeek Credential Setup
- Cost & Budget Formula
- Critics/Judges (ChatEval)
- Coordinator vs Semantic Arbiter
- Structured Outputs & Verifiers
- LLM-Blender Synthesizer
- Self-Consistency Proposers
- App Init Rationale
- Persistence & Backup Config
- Design QA Status
- API Endpoints Spec
- Drag-and-Drop HTMX
- Health Checks Spec
- Model Discovery Spec
- Tech Stack Overview
- Failure Recovery Spec
- Repository Design Note
- Multi-Agent Security
- Acceptance Tests
- Directory Structure
- Network Setup
- Packaging Guide
- AI Broker Package
- Health Checks (README)
- Project Overview
- Resource Planning

## God Nodes (most connected - your core abstractions)
1. `TaskCreateRequest` - 144 edges
2. `BrokerConfig` - 130 edges
3. `ModelReference` - 72 edges
4. `ProviderError` - 62 edges
5. `create_app()` - 60 edges
6. `ProcessingConfig` - 55 edges
7. `RoutedModelProvider` - 51 edges
8. `ProviderError` - 49 edges
9. `ConsensusCoordinator` - 46 edges
10. `OpenAICompatibleProviderConfig` - 45 edges

## Surprising Connections (you probably didn't know these)
- `prompt_tester.html (Probador de Prompts)` --semantically_similar_to--> `Selección auto/manual/hybrid`  [INFERRED] [semantically similar]
  app/templates/prompt_tester.html → AI_Broker_consenso_multi_LLM.md
- `Traducción de prompts a Ollama/DeepSeek` --shares_data_with--> `RoutedModelProvider`  [EXTRACTED]
  docs/Phase_4_Inference.md → app/providers/routing.py
- `Pipeline determinista de preprocesado (app/prompt_compressor.py)` --shares_data_with--> `RoutedModelProvider`  [EXTRACTED]
  docs/Prompt_Compression.md → app/providers/routing.py
- `select_optimal_model (matriz de enrutamiento)` --semantically_similar_to--> `Model Router`  [INFERRED] [semantically similar]
  Agent_AI_Broker.md → AI_Broker_consenso_multi_LLM.md
- `Fórmula de coste DeepSeek` --semantically_similar_to--> `Presupuesto y latencia (estimated_total, layer_latency)`  [INFERRED] [semantically similar]
  Agent_AI_Broker.md → AI_Broker_consenso_multi_LLM.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Restricción de un único workflow activo (diseño, contrato normativo, config y despliegue)** — ai_broker_consenso_multi_llm_single_workflow_constraint, agent_ai_broker_scheduler_concurrency, readme_single_workflow_constraint, broker_config_processing, deployment_guide_single_task_concurrency_note [INFERRED 0.85]
- **Gestión de credenciales vía keyring / Windows Credential Manager** — agent_ai_broker_security_operational, deployment_guide_keyring_setup, readme_deepseek_activation, broker_config_providers_deepseek, broker_config_server [INFERRED 0.85]
- **Patrón de fragmentos HTMX del panel operativo** — app_templates_dashboard, app_templates_fragments_summary, app_templates_fragments_queue, app_templates_fragments_active, app_templates_fragments_health, app_templates_fragments_resources, app_templates_fragments_config, app_templates_fragments_history [EXTRACTED 1.00]
- **Corpus de investigación sobre el catálogo de modelos de Ollama** — docs_deep_research_report_document, docs_estudio_de_modelos_en_ollama_document, docs_informe_de_decision_sobre_los_modelos_de_ollama_document [INFERRED 0.85]
- **Flujo de features del dashboard operativo (Operación, Read Models, Probador)** — docs_phase_5_dashboard_document, docs_phase_5_read_models_document, docs_prompt_tester_document [INFERRED 0.85]
- **Grupo de documentos que describen el invariante de concurrencia mixture_of_agents/slow** — docs_mixture_slow_concurrency_execution_preset_slow, docs_phase_5_dashboard_concurrency_invariant, docs_prompt_tester_mixture_manual_selection [INFERRED 0.85]

## Communities (64 total, 25 thin omitted)

### Community 0 - "Configuration Schema"
Cohesion: 0.06
Nodes (58): DeepSeekConfig, HealthConfig, HuggingFaceLocalConfig, HuggingFaceLocalModelConfig, LoggingConfig, OllamaConfig, OpenAICompatibleModelConfig, OpenAICompatibleProviderConfig (+50 more)

### Community 1 - "Provider Base Layer"
Cohesion: 0.06
Nodes (41): _CatalogCache, context_fits_with_capped_output(), CredentialResolver, enforce_context_limit(), estimate_required_context(), estimate_tokens_upper_bound(), _estimation_text(), infer_openai_compatible_capabilities() (+33 more)

### Community 2 - "Config Loading & Admin Auth"
Cohesion: 0.08
Nodes (88): admin_cookie_value(), Cookie de sesión `ts.hmac(token, ts)`: expira server-side y no expone el token., BrokerConfig, _deep_merge(), load_config(), PersistenceConfig, ProcessingConfig, Any (+80 more)

### Community 3 - "Admin Auth & Login Throttle"
Cohesion: 0.07
Nodes (52): _KeyringTokenCache, LoginThrottle, Backoff exponencial de intentos de login fallidos por origen., Evita consultar el backend de credenciales del SO en cada mutación., _apply_config_update(), _apply_probe_results(), _auto_or_int_field(), _build_dashboard_config() (+44 more)

### Community 4 - "Ollama Model Research"
Cohesion: 0.06
Nodes (44): Command R7B / Command A / Command R+ (Cohere), DeepSeek V4 Flash/Pro (cloud), Informe de decisión sobre los modelos de Ollama (deep research), Gemini 3 Flash Preview (Google, cloud), Gemma 4 (Google DeepMind), GLM-5.2 (Z.ai, cloud), GPT-OSS 20B/120B (OpenAI), Granite 4.1 (IBM) (+36 more)

### Community 5 - "App Bootstrap & Health"
Cohesion: 0.09
Nodes (34): _auto_start_local_provider_servers(), _detect_total_vram_gb(), _ensure_lmstudio_server(), _health_response(), _model_availability_item(), _model_feature_profile(), Any, VRAM total de las GPU NVIDIA visibles, o None si no se puede detectar (cacheado) (+26 more)

### Community 6 - "Task Repository"
Cohesion: 0.16
Nodes (17): dumps_json(), IdempotencyConflict, _invocation_window(), Any, datetime, ModelOutput, ValueError, QueueFull (+9 more)

### Community 7 - "Concurrency & Provider Design"
Cohesion: 0.06
Nodes (34): Criterios de aceptación de concurrencia slow, Ejecución async de waves con asyncio.TaskGroup, GET /api/v1/capabilities — negociación de contrato, Mixture of Agents /slow — Contrato de Concurrencia, ExecutionPreset 'slow', Error PARALLEL_CAPACITY_INSUFFICIENT, Persistencia y observabilidad de invocaciones (started_at/completed_at), Admisión atómica de VRAM, coste y leases por wave (+26 more)

### Community 8 - "Consensus Coordinator"
Cohesion: 0.15
Nodes (7): ConsensusCoordinator, Any, ModelOutput, Un fallo de disco al persistir un artefacto no debe tirar una tarea ya pagada., _dispatcher_loop(), Event, ProviderError

### Community 9 - "Dashboard Query Repository"
Cohesion: 0.20
Nodes (13): DashboardQueryRepository, _model_reference(), _percentile(), Any, Read-only projections for the dashboard and operational API., loads_json(), _parse_dt(), DashboardEventItem (+5 more)

### Community 10 - "Maintenance & Backup"
Cohesion: 0.23
Nodes (19): _artifact_files(), BackupResult, _cleanup_empty_dirs(), create_state_backup(), _file_record(), prune_terminal_task_artifacts(), prune_terminal_task_events(), Any (+11 more)

### Community 11 - "Resource Scheduler"
Cohesion: 0.24
Nodes (15): BaseModel, Enum, RuntimeError, str, ResourcePlan, ResourcePlanningError, ResourceScheduler, SchedulingMode (+7 more)

### Community 12 - "Dashboard Frontend JS"
Cohesion: 0.27
Nodes (17): bind(), csrfToken(), formPayload(), intervalFrom(), pollProbeProgress(), progressId(), refresh(), refreshDashboard() (+9 more)

### Community 13 - "Routed Model Provider"
Cohesion: 0.20
Nodes (5): Any, Busca el modelo exacto (nombre + deployment) o falla con el código adecuado., RoutedModelProvider, DeepSeekProvider, OllamaProvider

### Community 14 - "Prompt Compressor & Tests"
Cohesion: 0.21
Nodes (14): PromptCompressionConfig, PromptCompressor, Compresión determinista por reglas; sin llamadas a modelos., test_aggressive_drops_articles(), test_broker_config_defaults_and_toggle(), test_code_blocks_and_urls_preserved(), test_disabled_returns_original(), test_invalid_level_rejected() (+6 more)

### Community 15 - "Database Layer"
Cohesion: 0.23
Nodes (5): Database, Any, Path, Cursor, Row

### Community 16 - "Dashboard Template Filters"
Cohesion: 0.38
Nodes (13): _compatibility(), gb(), model_compatibility_class(), model_compatibility_label(), model_compatibility_text(), model_value(), ms(), Any (+5 more)

### Community 17 - "Multi-LLM Consensus Design"
Cohesion: 0.15
Nodes (13): Fórmula consensus_confidence, Protocolo de consenso (6 etapas), Plan de implementación (Fases A-E), mixture_of_agents/fast, mixture_of_agents/slow, Ollama /api/ps y keep_alive docs, Límites del debate multiagente (arXiv:2511.07784 / 2502.08788), Sesgos de jueces LLM (arXiv:2406.07791 / 2410.02736) (+5 more)

### Community 18 - "Admin Auth API Routes"
Cohesion: 0.21
Nodes (11): APIRouter, Request, Resolución y verificación del token de administración (dashboard y API)., Devuelve el token admin desde env (siempre fresco) o keyring (con caché TTL)., Exige credencial admin solo cuando hay token configurado (env o keyring)., resolve_admin_token(), verify_admin_access(), _verify_admin_cookie() (+3 more)

### Community 19 - "Dashboard Templates & Docs"
Cohesion: 0.21
Nodes (12): dashboard.html (panel operativo), fragments/active.html (tarea activa), fragments/health.html (salud de dependencias), fragments/history.html (historial de tareas), fragments/queue.html (cola de tareas), fragments/summary.html (métricas resumen), login.html (acceso de administración), Alcance del Design QA — panel 5.2 (+4 more)

### Community 20 - "Inference Contract & Compression Refs"
Cohesion: 0.17
Nodes (12): Preflight de contexto conservador, Fase 4 — Inferencia Transparente y Resultados, TaskCreateRequest.inference_kind (chat/embedding), Confirmación transaccional SQLite en estrategia single, Traducción de prompts a Ollama/DeepSeek, caveman-micro (proyecto externo, GitHub kuba-guzik), caveman (proyecto externo, GitHub JuliusBrussee), broker_config.yaml: prompt_compression (enabled/level/min_chars) (+4 more)

### Community 21 - "Agent Broker Contract Sections"
Cohesion: 0.20
Nodes (10): API asíncrona y durable, Cancelación y VRAM (Semaphore(1), leases, /api/ps), Contrato Normativo del MVP, Independencia de dominio, HealthSupervisor y servicio siempre encendido, Recuperación y pruebas de aceptación, Planificador, concurrencia interna y no bloqueo de la API, Seguridad operativa del MVP (keyring, sin auth LAN) (+2 more)

### Community 22 - "Prompt Compressor Implementation"
Cohesion: 0.20
Nodes (5): _compile_phrase_pattern(), CompressionResult, Compresión de prompts antes de la inferencia.  Adapta a español las técnicas de, Atajo: devuelve el texto comprimido (o el original si no aplica)., Pattern

### Community 23 - "Provider Generate/Propose/Synthesize"
Cohesion: 0.27
Nodes (4): neutralize_consensus_delimiters(), Impide que el contenido de un candidato cierre/abra los tags del árbitro.      S, ModelOutput, Prompt que viaja al proveedor; el original persiste intacto en la tarea.

### Community 24 - "Bootstrap Model Provider"
Cohesion: 0.33
Nodes (3): BootstrapModelProvider, Any, ModelOutput

### Community 25 - "Config & Resources Templates"
Cohesion: 0.28
Nodes (9): fragments/config.html (formulario de configuración), fragments/resources.html (recursos locales/VRAM), health: intervalos de comprobación, logging: JSON Lines con rotación, providers.custom: lmstudio (openai_compatible), providers.huggingface_local, resources: local_vram_budget_gb, safety_margin, waves, server: host/port/workers/cors/admin_token (+1 more)

### Community 26 - "Prompt Tester & Dashboard Docs"
Cohesion: 0.33
Nodes (7): Layout histórico del dashboard (superado por fase 5), Probador de Prompts — fase 5, Dashboard de consenso (Probador de Prompts, fase 5), docs/Mixture_Slow_Concurrency.md (concurrencia interna slow), docs/Phase_5_Dashboard.md (especificación normativa fase 5), docs/Prompt_Tester.md (especificación operativa del probador), Probador de Prompts — fase 5.3

### Community 27 - "Single Workflow Constraint"
Cohesion: 0.38
Nodes (7): Restricción de un único workflow activo, processing: max_active_workflows=1, max_parallel_invocations=auto, YT_Capture_Plugin (extensión Chrome), Knowledge Orchestrator (máquina principal), Nota de concurrencia: una sola tarea LLM activa, YouTube Knowledge Pipeline (sistema completo), Un solo workflow activo global (README)

### Community 28 - "Artifact Storage"
Cohesion: 0.43
Nodes (3): ArtifactRecord, ArtifactStore, Path

### Community 29 - "Logging Configuration"
Cohesion: 0.38
Nodes (5): configure_logging(), JsonLineFormatter, Path, _redact(), LogRecord

### Community 30 - "Model Comparison Templates"
Cohesion: 0.43
Nodes (7): comparison.html (vista Comparador), models.html (catálogo de modelos), prompt_tester.html (Probador de Prompts), task_detail.html (detalle de tarea), providers.custom: nvidia NIM (catálogo extenso de modelos), providers.ollama, Comparador de tareas mixture — fase 5.4

### Community 31 - "Model Selection & Routing"
Cohesion: 0.40
Nodes (5): call_deepseek_api, select_optimal_model (matriz de enrutamiento), Model Router, Selección auto/manual/hybrid, Selección de modelos (auto/manual/hybrid)

### Community 32 - "Prompt Compression Config"
Cohesion: 0.50
Nodes (5): Enrutamiento y contexto (fase 4), prompt_compression: enabled/level/min_chars, docs/Prompt_Compression.md (especificación de compresión de prompts), Compresión de prompts, Inferencia transparente (chat/embedding, sin truncar)

### Community 33 - "Artifact Persistence Schema"
Cohesion: 0.67
Nodes (3): Artefactos Markdown por modelo, Esquema de persistencia (tasks, consensus_runs, stages, model_invocations, candidate_evaluations, verification_results, events), Artefactos con integridad criptográfica (SHA-256)

### Community 34 - "DeepSeek Credential Setup"
Cohesion: 1.00
Nodes (3): providers.deepseek (keyring, budget), Registro de credenciales con keyring, Activación de DeepSeek (quickstart)

## Knowledge Gaps
- **98 isolated node(s):** `ai-broker`, `Proposers`, `Critics/Judges`, `Synthesizer (síntesis arbitral)`, `Verifiers (verificación objetiva)` (+93 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **25 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TaskCreateRequest` connect `Configuration Schema` to `Provider Base Layer`, `Admin Auth & Login Throttle`, `App Bootstrap & Health`, `Task Repository`, `Consensus Coordinator`, `Resource Scheduler`, `Routed Model Provider`, `Provider Generate/Propose/Synthesize`, `Bootstrap Model Provider`?**
  _High betweenness centrality (0.113) - this node is a cross-community bridge._
- **Why does `BrokerConfig` connect `Config Loading & Admin Auth` to `Configuration Schema`, `Provider Base Layer`, `Admin Auth & Login Throttle`, `App Bootstrap & Health`, `Resource Scheduler`, `Routed Model Provider`, `Prompt Compressor & Tests`, `Admin Auth API Routes`?**
  _High betweenness centrality (0.096) - this node is a cross-community bridge._
- **Why does `ProviderError` connect `Provider Base Layer` to `Configuration Schema`, `Config Loading & Admin Auth`, `Admin Auth & Login Throttle`, `Task Repository`, `Resource Scheduler`, `Routed Model Provider`, `Provider Generate/Propose/Synthesize`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Are the 41 inferred relationships involving `TaskCreateRequest` (e.g. with `ConsensusCoordinator` and `PromptTesterError`) actually correct?**
  _`TaskCreateRequest` has 41 INFERRED edges - model-reasoned connections that need verification._
- **Are the 38 inferred relationships involving `BrokerConfig` (e.g. with `_KeyringTokenCache` and `LoginThrottle`) actually correct?**
  _`BrokerConfig` has 38 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `ModelReference` (e.g. with `ConsensusCoordinator` and `DashboardQueryRepository`) actually correct?**
  _`ModelReference` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `ProviderError` (e.g. with `InferenceKind` and `OutputFormat`) actually correct?**
  _`ProviderError` has 16 INFERRED edges - model-reasoned connections that need verification._