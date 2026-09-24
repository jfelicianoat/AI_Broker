# ROL
Eres un ingeniero Python sénior trabajando en el repositorio AI_Broker (FastAPI + SQLite, Pydantic v2,
mypy con python_version 3.10, ruff line-length 120, cobertura mínima 90 %). Implementas EXACTAMENTE esta
especificación. No tomas decisiones de diseño propias. Si algo no está especificado, es ambiguo o el código
real contradice esta spec (un nombre, una firma o un comportamiento distinto del descrito), PARAS, describes
la discrepancia y esperas instrucciones. Nunca rellenes un hueco por intuición.

# OBJETIVO REAL
Que una tarea no muera porque falle un modelo o un proveedor cuando existe otra ruta válida, y que el broker
siga siendo plenamente autónomo con proveedores locales aunque todos los remotos (cloud/api) desaparezcan.
Lo local es el suelo; el failover remoto es una optimización encima.

# CONTEXTO VERIFICADO (léelo y confírmalo en el código antes de la fase 1)
- app/coordinator.py `Coordinator._single_inference`: reintenta el MISMO modelo 3 veces (`retry_delays = (0.5, 1.0)`).
  Lo llaman `_process_single` y `_process_single_with_escalation`.
- app/providers/base.py `provider_error_from_http`: sin diagnóstico usa `retryable=status >= 500`; no guarda
  Retry-After (solo lo hace `rate_limited_error`, usado en sondeos).
- app/providers/diagnostics.py `diagnose()`: clasifica solo por texto; la regla CREDENTIALS_REJECTED va antes que
  RATE_LIMITED y su regex `invalid.*token` convierte "Invalid value for max_tokens" en CREDENTIALS_REJECTED.
- app/providers/routing.py: `RoutedModelProvider.select(request, count, roles)`, `eligible_catalog(request)`,
  `_without_quarantined`, `_rank_candidates`, `local_footprints`, `ensure_agent_capable`, `models()`
  (añade `quarantined`/`quarantine_reason` a cada entrada) y la función fábrica que recibe `quarantine_loader`.
  app/main.py cablea `quarantine_loader=lambda: load_quarantine(db, ...)`.
- app/providers/bootstrap.py `BootstrapModelProvider.select`. Varios tests definen fakes con
  `select(self, request, count, roles)` y algunos sustituyen `client.app.state.coordinator.provider` tras crear la app.
- app/schemas.py: `is_local_deployment()` con LOCAL_DEPLOYMENTS = {"local", "bootstrap"}; `deployment` admite
  "cloud" | "api" | "local"; `TaskExecutionSummary`; `SelectionMode` (auto|manual|hybrid); `SelectionPolicy.allow_substitution`.
- app/coordinator.py: `process_task` calcula `effective_timeout` y ejecuta `asyncio.wait_for`; los códigos
  VRAM_INSUFFICIENT y LOCAL_MODEL_SLOTS_BUSY son señales de re-encolado (`_defer_for_memory`), no fallos.
  También existen `_TASK_FATAL_ERRORS`, `_process_consensus` (quórum = min(2, len(proposers))), `_run_proposer_wave`,
  `_synthesize_with_recovery`, `_substitute_arbiter`, `_process_single_map_reduce`, `_map_reduce_invocation`,
  `_process_agent`, `_run_agent_loop`, `AgentLoopResult`, `_enforce_budget`, `_with_remaining_budget`,
  `_run_cancellable`, `_attach_error_context`, `_context_window_for`, `_fit_agent_conversation`.
- app/providers/base.py también lanza CONTEXT_LIMIT_EXCEEDED DENTRO de propose, con details["context_window"].
- app/model_stats.py cuenta todas las invocaciones `failed`; app/model_quarantine.py deriva de `model_invocations`.
- Contrato "2.10" en app/main.py. `TaskExecutionSummary` se construye en app/repository.py a partir de `result`.
- app/db.py: `CREATE TABLE IF NOT EXISTS` en `init_schema`; API `execute`, `query_one`, `query_all`, `transaction`.
- CI: `ruff check .`, `mypy`, `pytest --cov=app --cov-report=term-missing`.

# DEFINICIONES
- Dominio: "local" si is_local_deployment(deployment); en otro caso, "remote".
- Identidad de modelo: (provider.lower(), deployment.lower(), model.lower()).
- Agregador: proveedor openai_compatible con `aggregator: true` (p. ej. FreeLLMAPI). Siempre es "remote".
- Salto (hop): intento fallido tras el que se cambia de modelo. Un retry_same NO es un salto.
- Clase 5xx: code == "MODEL_ERROR" y details["http_status"] == 408 o >= 500.

# REGLAS GLOBALES (prioridad: 1 > 2 > 3 > 4)
1. Toda sustitución sale de `select()`. El failover nunca viola cloud_allowed, allowed_providers, la frontera de
   datos, las capacidades, la visión, el contexto, task_affinity ni max_cost_usd.
2. `fallback_allowed=False`: se permite retry_same; nunca se cambia de modelo; se propaga el error.
3. Un error lanzado por `select()` nunca se clasifica. En el primer intento se propaga como hoy; después se re-lanza
   el último error de invocación (ver `_select_failover_candidate`).
4. NO modificar: broker_config.yaml, la cuarentena, `_judge_confidence`, los sondeos `probe_*`, shadow_probe,
   el turno de cierre del agente ni la lógica de sustitución del árbitro (salvo lo indicado en F6).
5. Tests existentes: no modificar ninguno salvo los de la sección TESTS EXISTENTES AUTORIZADOS. Si otro falla, PARA.
6. Identificadores en inglés; comentarios y docstrings en español, explicando el porqué como el resto del repo.
   Tipado completo; nada de sintaxis exclusiva de 3.12+.
7. Git: trabaja en la rama `feature/failover`, con un commit por fase. Nunca push, rebase ni force.
8. Nunca afirmes que algo está verificado sin haberlo ejecutado. Separa "verificado" de "no verificado".

# PROCESO
Antes de cada fase: lee los ficheros que toca y confirma que los nombres y las firmas citados existen.
Después implementa, ejecuta `ruff check .`, `mypy` y `pytest --cov=app --cov-report=term-missing`, haz el commit,
entrega el INFORME DE FASE y DETENTE hasta que el usuario escriba "continúa".
Criterio común a todas las fases: ruff y mypy sin errores, suite completa en verde, cobertura total >= 90 %
y cada módulo nuevo >= 90 %.

====================================================================================================
FASE 1 — Clasificación por código HTTP y detalle de transporte
====================================================================================================
Ficheros: app/providers/diagnostics.py, app/providers/base.py, app/providers/ollama.py,
app/providers/deepseek.py, app/providers/openai_compatible.py.

1.1 diagnostics.py
 a) En `_SIGNATURES`, mueve la entrada RATE_LIMITED delante de CREDENTIALS_REJECTED.
 b) Sustituye la regex de CREDENTIALS_REJECTED por:
    r"\bAPI key\b|unauthorized|invalid[ _-]?(?:api[ _-]?)?key|invalid.*(?:access|auth|bearer)[ _-]?token|authentication"
 c) Añade las constantes de módulo (se comparan contra raw.lower()):
    CREDIT_MARKERS = ("insufficient_quota", "insufficient credit", "insufficient_credit", "insufficient balance",
                      "insufficient_balance", "payment required", "requires more credits", "credit balance is too low")
    SUSPENDED_MARKERS = ("account suspended", "account has been suspended", "account is suspended",
                         "account disabled", "account has been disabled")
    DAILY_QUOTA_PATTERN = re.compile(r"\b(?:per[ _-]day|daily|rpd|tpd)\b", re.I)
    DEGRADED_MARKER = "degraded function"
 d) Extrae la Diagnosis RATE_LIMITED actual a una constante `_RATE_LIMITED` (reutilizada en `_SIGNATURES`) y la
    Diagnosis CREDENTIALS_REJECTED a `_CREDENTIALS_REJECTED`. Añade tres constantes nuevas con este texto exacto:
    _OUT_OF_CREDITS = Diagnosis("OUT_OF_CREDITS", "el proveedor rechazó la petición por falta de saldo o cuota de pago.",
        "Recarga el saldo o revisa el plan de la cuenta; mientras tanto el broker usará otras rutas.", retryable=False)
    _DAILY_QUOTA = Diagnosis("DAILY_QUOTA_EXHAUSTED", "el proveedor agotó la cuota diaria de este modelo.",
        "Vuelve a estar disponible cuando el proveedor reinicie la cuota (Retry-After o medianoche UTC).", retryable=True)
    _MODEL_FORBIDDEN = Diagnosis("MODEL_ACCESS_FORBIDDEN", "la cuenta no tiene acceso a este modelo.",
        "Suele ser un modelo fuera del plan o del tier gratuito: usa otro modelo o amplía el plan.", retryable=False)
 e) Nueva función `classify_http_failure(status: int, raw: str) -> Diagnosis | None`:
    402 -> _OUT_OF_CREDITS
    401 -> _CREDENTIALS_REJECTED
    403 -> _OUT_OF_CREDITS si contiene algún CREDIT_MARKER o SUSPENDED_MARKER; si no, _MODEL_FORBIDDEN
    429 -> _OUT_OF_CREDITS si contiene algún CREDIT_MARKER; si no, _DAILY_QUOTA si casa DAILY_QUOTA_PATTERN;
           si no, _RATE_LIMITED
    cualquier otro status -> None
 f) `diagnose()` conserva su firma y su comportamiento, salvo por los cambios a) y b).

1.2 base.py `provider_error_from_http` (misma firma). Nuevo cuerpo lógico:
    raw = provider_http_error_message(error); status = error.response.status_code
    details = {"provider": provider, "http_status": status, "provider_error": raw,
               "retry_after": retry_after_seconds(error.response)}
    by_status = classify_http_failure(status, raw)
    if by_status: return ProviderError(by_status.code, by_status.message(provider, model),
                                       retryable=by_status.retryable, details=details)
    diagnosis = diagnose(raw)
    if diagnosis is not None and diagnosis.code == "CREDENTIALS_REJECTED": diagnosis = None
    if diagnosis is None: return ProviderError(default_code, raw, retryable=status >= 500, details=details)
    return ProviderError(diagnosis.code, diagnosis.message(provider, model), retryable=diagnosis.retryable, details=details)

1.3 base.py: nueva función
    def transport_provider_error(error: httpx.HTTPError, *, provider: str) -> ProviderError
    transport = "connect" si isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
                "timeout" si isinstance(error, httpx.TimeoutException) (comprobado DESPUÉS de lo anterior)
                "network" en cualquier otro caso
    return ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True,
                         details={"provider": provider, "transport": transport})

1.4 Sustituye `raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error` por
    `raise transport_provider_error(error, provider=<id>) from error` SOLO en estas ramas
    `except (httpx.TimeoutException, httpx.NetworkError)`:
    - ollama.py: `generate`, `chat_tools`, `embed` (id "ollama")
    - deepseek.py: `generate`, `chat_tools` (id "deepseek")
    - openai_compatible.py: `generate`, `chat_tools`, `embed` (id self.config.id)
    No toques ramas de sondeo, catálogo ni descarga.

1.5 Tests nuevos: tests/test_failover_classification.py (patrón de construcción de httpx.HTTPStatusError igual
    que tests/test_error_diagnostics.py). Cada caso es un test independiente:
    - 402 "This request requires more credits" -> OUT_OF_CREDITS, retryable False
    - 429 {"code":"insufficient_quota"} -> OUT_OF_CREDITS
    - 429 "API key rate limit exceeded" -> RATE_LIMITED, retryable True
    - 429 "Rate limit reached for requests per day (RPD)" -> DAILY_QUOTA_EXHAUSTED
    - 429 con cabecera Retry-After: 63 -> details["retry_after"] == 63.0; sin cabecera -> None
    - 400 "Invalid value for max_tokens: must be <= 8192" -> MODEL_ERROR, retryable False
    - diagnose("Invalid value for max_tokens: must be <= 8192") is None
    - 401 -> CREDENTIALS_REJECTED
    - 403 sin marcadores -> MODEL_ACCESS_FORBIDDEN; 403 "account suspended" -> OUT_OF_CREDITS
    - 500 "cudaMalloc failed: out of memory" -> MODEL_OUT_OF_MEMORY (regresión)
    - 503 no reconocido -> MODEL_ERROR, retryable True, con details["provider_error"] presente
    - transport_provider_error: ConnectError y ConnectTimeout -> "connect"; ReadTimeout -> "timeout";
      RemoteProtocolError -> "network"
    - OpenAICompatibleProvider.generate con MockTransport que lanza httpx.ConnectError ->
      details["transport"] == "connect"
Guía manual F1: con la API key de un proveedor cloud cambiada a un valor inválido, lanza una tarea single con
target_model en ese proveedor y fallback_allowed=false. Esperado: error CREDENTIALS_REJECTED con
details.http_status 401. Restaura la clave después.

====================================================================================================
FASE 2 — Restricciones de selección en el router
====================================================================================================
2.1 Nuevo fichero app/selection_constraints.py (solo stdlib; lo importa routing.py, así que NO puede importar
    nada de app.providers):
    @dataclass(frozen=True)
    class SelectionConstraints:
        exclude_models: frozenset[tuple[str, str, str]] = frozenset()   # identidades en minúsculas
        exclude_providers: frozenset[str] = frozenset()                 # minúsculas
        exclude_domains: frozenset[str] = frozenset()                   # subconjunto de {"local", "remote"}
        max_local_size_bytes: int | None = None
        min_context_window: int | None = None
        max_expected_seconds: float | None = None
        def is_empty(self) -> bool                          # True si todos los campos están en su valor por defecto
        def merged(self, other: "SelectionConstraints | None") -> "SelectionConstraints"
            # une los conjuntos; para max_local_size_bytes y max_expected_seconds, el menor de los no-None;
            # para min_context_window, el mayor de los no-None
        def allows(self, entry: Mapping[str, Any]) -> bool
            # False si: la identidad (provider, deployment, name) está en exclude_models; provider en exclude_providers;
            # el dominio está en exclude_domains (dominio local si deployment en {"local", "bootstrap"});
            # la entrada es local, max_local_size_bytes no es None, size_bytes > 0 y size_bytes > max_local_size_bytes;
            # min_context_window no es None y context_window es None o < min_context_window.
            # No evalúa max_expected_seconds.

2.2 routing.py
    - `eligible_catalog(self, request, constraints: SelectionConstraints | None = None)`: al final, si constraints no
      es None y no está vacío, filtra `catalog`, `capability_catalog` y `context_catalog` con `constraints.allows`.
    - `select(self, request, count, roles, *, constraints: SelectionConstraints | None = None)`:
      a) pasa constraints a `eligible_catalog`;
      b) si constraints.max_expected_seconds no es None y config.routing.adaptive_selection es True: justo después
         de obtener context_catalog y ANTES de cualquier otra lógica, elimina de context_catalog las entradas cuya
         estimación exista y sea > max_expected_seconds. Las entradas sin estimación se conservan.
         Si context_catalog tenía elementos y queda vacío por este filtro:
         raise ProviderError("FAILOVER_DEADLINE_EXCEEDED",
                             "Ningún candidato cabe en el tiempo que le queda a la tarea", retryable=False)
      c) la estimación se calcula con un helper privado nuevo
         `_estimated_seconds(entries, request, loaded) -> dict[int, float | None]` (clave id(entry)) que reutiliza
         `estimate_seconds` con los mismos argumentos que `_rank_candidates`. `_rank_candidates` no cambia.
      d) Decisión explícita: con target_model excluido y fallback_allowed=True, la rama preferred (que compara solo
         por nombre) PUEDE elegir el mismo nombre de modelo en otro proveedor. Es deseado; no lo impidas.
    - bootstrap.py `select`: añade `*, constraints: SelectionConstraints | None = None` y lo ignora.

2.3 Tests nuevos: tests/test_selection_constraints.py, usando el patrón `_router(...)` y `_CatalogStub` de
    tests/test_adaptive_routing.py (copia lo necesario; no lo importes). Casos:
    - exclude_models elimina exactamente esa identidad y el resto conserva su orden
    - exclude_providers elimina todas las entradas del proveedor
    - exclude_domains {"remote"} deja solo las locales
    - max_local_size_bytes elimina las locales mayores, conserva las de size 0 o sin size, y las remotas
    - min_context_window elimina las entradas sin ventana o con ventana menor
    - target excluido con fallback_allowed True devuelve otro modelo
    - el mismo nombre en otro proveedor entra por la rama preferred (caso d)
    - constraints=None y SelectionConstraints() vacío producen exactamente el mismo resultado que hoy
    - max_expected_seconds elimina las medidas por encima y conserva las no medidas;
      si todo queda fuera -> FAILOVER_DEADLINE_EXCEEDED
    - cloud_allowed False con exclude_domains {"local"} -> select lanza error y nunca devuelve un modelo cloud
Guía manual F2: sin cambios observables para el usuario; basta con la suite en verde.

====================================================================================================
FASE 3 — Motor de failover, single, contrato 2.11 y estadísticas limpias
====================================================================================================
3.1 app/config.py
    class RemoteFailoverConfig(BaseModel):
        max_hops: int = Field(default=6, ge=0, le=20)
        time_budget_seconds: float = Field(default=90.0, gt=0)
    class LocalFailoverConfig(BaseModel):
        max_hops: int = Field(default=2, ge=0, le=10)
        provider_escalation_threshold: int = Field(default=3, ge=1, le=20)
        retry_same_delay_seconds: float = Field(default=1.0, ge=0, le=30)
    class FailoverCooldownConfig(BaseModel):   # todos los campos con gt=0
        rate_limited_default_seconds: float = 60.0
        out_of_credits_seconds: float = 86400.0
        provider_unavailable_seconds: float = 120.0
        credentials_rejected_seconds: float = 3600.0
        model_forbidden_seconds: float = 86400.0
        model_not_found_seconds: float = 3600.0
        max_cooldown_seconds: float = 86400.0
    class FailoverConfig(BaseModel):
        enabled: bool = True
        remote: RemoteFailoverConfig = Field(default_factory=RemoteFailoverConfig)
        local: LocalFailoverConfig = Field(default_factory=LocalFailoverConfig)
        cooldowns: FailoverCooldownConfig = Field(default_factory=FailoverCooldownConfig)
    BrokerConfig.failover: FailoverConfig = Field(default_factory=FailoverConfig)
    OpenAICompatibleProviderConfig.aggregator: bool = False, con este validador:
      aggregator True y deployment == "local" -> ValueError("un agregador no puede ser local")
    Constantes nuevas, junto a UNUSABLE_OUTPUT_CODES:
    FAILOVER_FATAL_CODES = frozenset({"TASK_CANCELLED", "BUDGET_EXCEEDED", "CLOUD_NOT_ALLOWED", "PROVIDER_NOT_ALLOWED",
        "VRAM_INSUFFICIENT", "LOCAL_MODEL_SLOTS_BUSY", "CONTEXT_WINDOW_UNKNOWN", "ALL_ROUTES_COOLING_DOWN",
        "IMAGE_GENERATION_UNSUPPORTED", "VISION_MODEL_UNAVAILABLE"})
    CIRCUMSTANTIAL_FAILURE_CODES = ("RATE_LIMITED", "DAILY_QUOTA_EXHAUSTED", "OUT_OF_CREDITS", "CREDENTIALS_REJECTED",
        "TASK_CANCELLED", "BUDGET_EXCEEDED", "VRAM_INSUFFICIENT", "LOCAL_MODEL_SLOTS_BUSY", "ALL_ROUTES_COOLING_DOWN")

3.2 app/schemas.py
    class FailoverAttempt(StrictBaseModel):
        provider: str; deployment: str; model: str; role: str | None = None
        outcome: Literal["completed", "failed"]
        code: str | None = None; http_status: int | None = None
        action: Literal["retry_same", "switch", "fatal"] | None = None
        exclude: Literal["none", "model", "provider"] | None = None
        cooldown_seconds: float | None = None
    TaskExecutionSummary.failover_attempts: list[FailoverAttempt] = Field(default_factory=list)
    app/repository.py: al construir TaskExecutionSummary, rellena failover_attempts desde result["failover_attempts"]
    (valida cada dict con FailoverAttempt.model_validate; descarta en silencio los inválidos). No cambies la regla que
    decide cuándo omitir el resumen. `fallback_used` NO cambia de semántica.
    app/main.py: contract_version "2.11".

3.3 app/failover.py
    @dataclass(frozen=True)
    class FailoverDecision:
        action: Literal["retry_same", "switch", "fatal"]
        exclude: Literal["none", "model", "provider"]
        cooldown_scope: Literal["none", "model", "provider"]
        cooldown_seconds: float | None
        reason: str   # etiqueta corta: "rate_limited", "out_of_credits", "provider_down", "out_of_memory", "context", ...

    def classify_failover(error, *, domain, aggregator, cooldowns: FailoverCooldownConfig, now: datetime,
                          retry_same_available: bool) -> FailoverDecision
    Función pura. Se evalúan las filas en orden y gana la primera que case. Notación: RA = details["retry_after"] si
    es un número >= 0; MID = segundos hasta la próxima 00:00 UTC, con un mínimo de 60; todo cooldown se limita a
    max_cooldown_seconds.

    COMÚN (ambos dominios):
      F0  code en FAILOVER_FATAL_CODES -> fatal, none, none
      F1  code == "PROVIDER_UNAVAILABLE" y error.retryable is False -> switch, provider, sin cooldown   ("provider_disabled")

    REMOTE con aggregator=True:
      A1  RATE_LIMITED -> switch, provider, cooldown provider = RA o rate_limited_default_seconds
      A2  DAILY_QUOTA_EXHAUSTED -> switch, provider, cooldown provider = RA o MID
      A3  OUT_OF_CREDITS -> switch, provider, cooldown provider = out_of_credits_seconds
      A4  PROVIDER_UNAVAILABLE o clase 5xx -> switch, provider, cooldown provider = provider_unavailable_seconds
      A5  cualquier otro -> switch, provider, sin cooldown

    REMOTE sin agregador:
      R1  OUT_OF_CREDITS -> switch, provider, cooldown provider = out_of_credits_seconds
      R2  CREDENTIALS_REJECTED -> switch, provider, cooldown provider = credentials_rejected_seconds
      R3  RATE_LIMITED -> switch, model, cooldown model = RA o rate_limited_default_seconds
      R4  DAILY_QUOTA_EXHAUSTED -> switch, model, cooldown model = RA o MID
      R5  MODEL_ACCESS_FORBIDDEN -> switch, model, cooldown model = model_forbidden_seconds
      R6  MODEL_ERROR con http_status 404 o 410 -> switch, model, cooldown model = model_not_found_seconds
      R7  PROVIDER_UNAVAILABLE, clase 5xx, o DEGRADED_MARKER en (details["provider_error"] o str(error)).lower()
          -> switch, provider, cooldown provider = provider_unavailable_seconds
      R8  CONTEXT_LIMIT_EXCEEDED, o MODEL_ERROR con http_status 413 -> switch, model, sin cooldown   ("context")
      R9  cualquier otro -> switch, model, sin cooldown

    LOCAL (nunca lleva cooldown):
      L1  PROVIDER_UNAVAILABLE con transport "connect" -> switch, provider
      L2  PROVIDER_UNAVAILABLE con transport "timeout" -> switch, model
      L3  PROVIDER_UNAVAILABLE (con cualquier otro transport o sin él), PROVIDER_ENGINE_CRASHED, MODEL_NOT_LOADED
          o clase 5xx -> retry_same si retry_same_available; si no, switch, model   ("provider_down")
      L4  MODEL_OUT_OF_MEMORY o VRAM_MODEL_TOO_LARGE -> switch, model   ("out_of_memory")
      L5  CONTEXT_LIMIT_EXCEEDED, o MODEL_ERROR con http_status 413 -> switch, model   ("context")
      L6  cualquier otro -> switch, model

    class FailoverSession:
        __init__(self, config: FailoverConfig, *, fallback_allowed: bool, deadline_monotonic: float | None,
                 clock: Callable[[], float] = time.monotonic)
        Atributos: attempts: list[FailoverAttempt]; spent_outputs: list[ModelOutput]; current_model: ModelReference | None;
          stop_reason: str | None; retry_same_used: bool; hops = {"local": 0, "remote": 0};
          remote_started_at: float | None; y los acumuladores de exclusión (modelos, proveedores, dominios,
          max_local_size_bytes, min_context_window, y fallos de proveedor local por proveedor como
          dict[str, set[identidad]]).
        note_attempt_start(model): si el dominio es remote y remote_started_at es None, lo fija a clock().
        constraints() -> SelectionConstraints con todas las exclusiones y
          max_expected_seconds = deadline_monotonic - clock() (None si no hay deadline).
        is_excluded(model) -> bool   (identidad, proveedor o dominio excluidos)
        record_success(model): añade el intento completed y fija current_model.
        record_failure(model, error, decision, *, role, size_bytes, context_window):
          - añade un FailoverAttempt failed (code, http_status, action, exclude, cooldown_seconds);
            si error.output no es None, lo añade a spent_outputs
          - exclude "model" -> añade la identidad; "provider" -> añade el proveedor
          - reason "out_of_memory" y size_bytes > 0 -> max_local_size_bytes = min(actual, size_bytes - 1)
          - reason "context" -> ventana = details["context_window"] si es int; si no, el argumento context_window;
            si existe, min_context_window = max(actual, ventana + 1)
          - dominio local y reason "provider_down" con action switch -> registra la identidad bajo su proveedor;
            si ese proveedor acumula >= local.provider_escalation_threshold identidades, excluye el proveedor
          - action "retry_same" -> retry_same_used = True
          - action "switch" -> hops[dominio] += 1; si hops[dominio] >= max_hops de ese dominio, excluye el dominio
          - dominio remote y clock() - remote_started_at >= remote.time_budget_seconds -> excluye "remote"
        can_continue() -> bool: len(attempts) < remote.max_hops + local.max_hops + 2 y
          (deadline_monotonic es None o clock() < deadline_monotonic)
        has_failures() -> bool

3.4 app/coordinator.py
    - Variable de módulo `_TASK_DEADLINE: ContextVar[float | None] = ContextVar("_TASK_DEADLINE", default=None)`.
      En `process_task`, justo antes de `asyncio.wait_for(...)`: token = _TASK_DEADLINE.set(time.monotonic() + effective_timeout);
      en un `finally`, _TASK_DEADLINE.reset(token).
    - `_new_failover_session(request)`, con fallback_allowed = request.model_requirements.fallback_allowed y
      deadline = _TASK_DEADLINE.get().
    - `_select_accepts_constraints() -> bool`: "constraints" in inspect.signature(self.provider.select).parameters,
      evaluado EN CADA LLAMADA (sin caché: los tests sustituyen el proveedor en caliente).
    - `_is_aggregator(model)`: getattr(self.provider, "provider_is_aggregator", None); False si no existe.
      Añade a RoutedModelProvider `provider_is_aggregator(provider_id: str) -> bool` (busca en las configs custom,
      sin distinguir mayúsculas).
    - `async _model_facts(model) -> tuple[int | None, int | None]`: size_bytes vía `local_footprints([model])` si el
      modelo es local y el método existe; context_window vía `_context_window_for(model)`.
    - `async _handle_invocation_failure(repository, task_id, model, error, session, *, role) -> FailoverDecision`
      1. si config.failover.enabled es False -> decision fatal, stop_reason "disabled"
      2. si no, decision = classify_failover(..., retry_same_available=(dominio == "local" and not session.retry_same_used))
      3. size, window = await _model_facts(model)
      4. session.record_failure(...)
      5. evento "failover.attempt_failed" con el FailoverAttempt, y logger.warning con el mismo nombre
      6. convierte la decisión en fatal (con este stop_reason) si: action fatal ("fatal_code");
         error.code == "TASK_CANCELLED" o repository.is_cancel_requested(task_id) ("cancelled");
         not session.fallback_allowed y action != "retry_same" ("fallback_not_allowed");
         not session.can_continue() ("max_attempts" o "deadline")
      7. devuelve la decisión resultante
    - `_decorate_failover_error(error, session)`: si len(session.attempts) >= 2, añade (sin sustituir las claves que
      ya existan) error.details["failover_attempts"] = [a.model_dump(mode="json") ...] y
      error.details["failover_stop"] = session.stop_reason.
    - `async _select_failover_candidate(request, role, session, base_constraints, last_error) -> ModelReference`
      constraints = session.constraints().merged(base_constraints)
      si not _select_accepts_constraints(): stop_reason "select_without_constraints", decora y lanza last_error
      try: picked = (await self.provider.select(request, 1, [role], constraints=constraints))[0]
      except ProviderError as e: stop_reason f"no_candidate:{e.code}", decora y lanza last_error
      si session.is_excluded(picked): stop_reason "no_new_candidate", decora y lanza last_error
      evento "failover.switched" {"from": identidad del último intento, "to": identidad de picked}; devuelve picked
    - `async _invoke_with_failover(repository, task_id, request, *, role, run_id, stage, session, first_model,
          base_constraints, call, on_attempt=None, complete_on_success: bool) -> tuple[ModelReference, ModelOutput, str]`
      model = first_model; last_error = None
      bucle:
        si model es None:
          si last_error es None: model = (await self.provider.select(request, 1, [role]))[0]   # llamada idéntica a la actual
          si no: model = await _select_failover_candidate(...)
        self._enforce_budget(request, session.spent_outputs)
        attempt_request = self._with_remaining_budget(request, session.spent_outputs) si hay spent_outputs; si no, request
        si on_attempt: on_attempt(model)
        session.note_attempt_start(model)
        invocation_id = repository.start_invocation(task_id, run_id, role, model, classify_task_type(request),
            await self._model_loaded_state(model), excluded_from_learning=request.exclude_from_model_learning,
            fingerprint=await self._invocation_fingerprint(model), compression=self._compression_echo(attempt_request))
        try: output = await self._run_cancellable(repository, task_id, call(attempt_request, model))
        except ProviderError as error:
          self._attach_error_context(error, stage, model, role=role)
          repository.fail_invocation(invocation_id, task_id, error.code, str(error), error.output)
          decision = await self._handle_invocation_failure(...)
          si decision.action == "fatal": _decorate_failover_error(error, session); raise
          last_error = error
          si decision.action == "retry_same": await asyncio.sleep(config.failover.local.retry_same_delay_seconds); continue
          model = None; continue
        si complete_on_success: repository.complete_invocation(invocation_id, task_id, output)
        session.record_success(model); return model, output, invocation_id

3.5 `_single_inference` reescrito sobre `_invoke_with_failover`:
    role=role, run_id=None, stage="generating", first_model=None, base_constraints=None,
    call=lambda req, m: self.provider.propose(req, m, 1), complete_on_success=False.
    on_attempt hace el mismo repository.update_task(... TaskStatus.generating ..., clear_queue_position=True) que hoy,
    con active_invocations=[modelo actual] y cost_actual_usd = suma de spent_outputs.
    Elimina retry_delays y el log "task.single_retry". El chequeo de cancelación posterior se mantiene igual.
    Nuevo retorno: tuple[ModelReference, ModelOutput, str, FailoverSession] | None.
    Actualiza `_process_single` y `_process_single_with_escalation` para desempaquetar 4 valores.
    `_finalize_single(..., session: FailoverSession | None = None)`: cost_actual_usd = output.cost_usd + la suma de
    spent_outputs; si session.has_failures(), result["failover_attempts"] = [a.model_dump(mode="json") ...].
    En la escalada a mixture, la sesión se descarta: los intentos ya quedaron como eventos.

3.6 app/model_stats.py: añade a la consulta este predicado (con parámetros ligados y COALESCE obligatorio):
    AND NOT (status = 'failed' AND (
          COALESCE(error_code, '') IN (<CIRCUMSTANTIAL_FAILURE_CODES>)
       OR (COALESCE(error_code, '') = 'PROVIDER_UNAVAILABLE' AND lower(deployment) NOT IN ('local', 'bootstrap'))))
    Motivo: la falta de cuota o de red no mide la calidad de un modelo, pero el timeout de un modelo local sí mide su lentitud.

3.7 Tests nuevos
    tests/failover_support.py: `ScriptedFailoverProvider(BootstrapModelProvider)`, con:
      - un catálogo fijo y ordenado de ModelReference (con dominio y aggregator configurables);
      - `select(request, count, roles, *, constraints=None)` que devuelve el primer modelo del orden que cumpla las
        exclusiones de constraints y, si no hay ninguno, lanza ProviderError("MODEL_UNAVAILABLE", ...);
      - `propose` guiado por un guion {nombre_modelo: [ProviderError | "ok", ...]} y un contador de llamadas por modelo;
      - `provider_is_aggregator(id)`.
    tests/test_failover_single.py (retry_same_delay_seconds = 0 en todos salvo en el test existente):
      - remoto A con 402 -> sirve B; failover_attempts = [A failed OUT_OF_CREDITS exclude provider, B completed]
      - 429 en A -> B del MISMO proveedor es elegible (exclusión de modelo, no de proveedor)
      - 503 en A (remoto) -> se salta todo su proveedor y sirve uno de otro proveedor
      - local: transport "connect" excluye el proveedor; "timeout" excluye solo el modelo
      - local: PROVIDER_ENGINE_CRASHED reintenta una vez el mismo modelo y después cambia
      - local: MODEL_OUT_OF_MEMORY excluye los locales con size_bytes >= el del fallido
      - CONTEXT_LIMIT_EXCEEDED lanzado por propose con details.context_window=1000 -> min_context_window 1001
      - agregador: RATE_LIMITED excluye el proveedor entero
      - remote.max_hops=1 -> tras 1 fallo remoto solo quedan locales
      - presupuesto remoto de 90 s agotado (reloj falso) -> no se inician más intentos remotos
      - fallback_allowed False -> un solo intento (más un retry_same si procede), se propaga el error
      - failover.enabled False -> un solo intento, sin retry_same
      - BUDGET_EXCEEDED, VRAM_INSUFFICIENT y TASK_CANCELLED -> sin failover
      - un fake SIN parámetro constraints -> sin cambio de modelo; error original y details intactos
      - todos los candidatos agotados -> se lanza el ÚLTIMO error con details.failover_attempts y failover_stop
      - estadísticas: los fallos RATE_LIMITED no penalizan la tasa de éxito; un PROVIDER_UNAVAILABLE local sí
      - GET /api/v1/tasks/{id}: execution_summary.failover_attempts poblado; fallback_used sin cambios
      - config: aggregator con deployment local -> ValueError

3.8 TESTS EXISTENTES AUTORIZADOS (solo en esta fase):
    - "2.10" -> "2.11" en las aserciones de contract_version de tests/test_api.py, tests/test_demonstrable_execution.py
      y tests/test_long_context.py (solo esas líneas)
    - tests/fixtures/broker_task_state_response.schema.json: añade execution_summary.properties.failover_attempts
      (array de objetos con las claves de FailoverAttempt)
    `test_single_strategy_retries_transient_provider_errors` y `test_single_strategy_does_not_retry_permanent_errors`
    deben pasar SIN cambios. Si no pasan, PARA.
Guía manual F3: tarea single con cloud permitido, contra un proveedor cloud con la clave inválida y otro válido.
Esperado: la tarea completa, execution_summary.failover_attempts muestra el fallo y el éxito, y el panel lista
dos invocaciones.

====================================================================================================
FASE 4 — Cooldown persistente (solo remoto) y autonomía local
====================================================================================================
4.1 app/db.py `init_schema`:
    CREATE TABLE IF NOT EXISTS route_cooldowns (
      provider TEXT NOT NULL, deployment TEXT NOT NULL, model TEXT NOT NULL,
      scope TEXT NOT NULL CHECK (scope IN ('model', 'provider')),
      error_code TEXT NOT NULL, http_status INTEGER, reason TEXT NOT NULL,
      expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY (provider, deployment, model))
    Con scope provider, model = '*'. Claves en minúsculas.
4.2 app/route_cooldown.py
    @dataclass(frozen=True) class CooldownEntry: scope, provider, deployment, model, error_code, http_status, reason,
      expires_at: datetime
    class CooldownStore(db, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
      set(*, scope, provider, deployment, model: str | None, error_code, http_status, reason, seconds) -> CooldownEntry
        borra primero las filas expiradas; hace upsert y conserva el expires_at MAYOR
      active() -> dict[tuple[str, str, str], CooldownEntry]   (solo expires_at > now)
      clear(provider, deployment, model) -> bool
4.3 routing.py: parámetro `cooldown_loader` en el constructor y en la fábrica, igual que quarantine_loader.
    app/main.py pasa `cooldown_loader=CooldownStore(db).active`.
    En eligible_catalog, justo después de `_without_quarantined`, llama a un nuevo `_without_cooling_down(catalog, request)`:
    elimina las entradas con cooldown de modelo (provider, deployment, name) o de proveedor (provider, deployment, "*"),
    EXCEPTO la entrada que coincide con target_model cuando fallback_allowed es False (un pin explícito manda).
    Si el catálogo tenía entradas y queda vacío:
      raise ProviderError("ALL_ROUTES_COOLING_DOWN", "Todas las rutas permitidas están en pausa hasta <iso>",
        retryable=True, details={"retry_after": segundos hasta la expiración más próxima,
        "cooldowns": [hasta 20 dicts {provider, deployment, model, error_code, expires_at}]})
    models(): junto a quarantined, añade a cada entrada "cooldown_until" (ISO o None) y "cooldown_reason"
    ("<error_code>: <reason>" o None), usando la expiración más tardía entre la del modelo y la del proveedor.
4.4 coordinator.py: en __init__, self.cooldowns = CooldownStore(self.db). En `_handle_invocation_failure`, entre los
    pasos 2 y 3: si el dominio es remote, cooldown_scope != "none" y cooldown_seconds > 0 -> self.cooldowns.set(...)
    y evento "failover.cooldown_set". En local, nunca.
4.5 Tests nuevos: tests/test_route_cooldown.py (reloj inyectado)
    - set/active/expiración; el upsert conserva el expires_at mayor; el '*' de proveedor bloquea todos sus modelos
    - prueba de autonomía: todos los modelos remotos fallan con transport "connect" y existe uno local válido.
      Tarea 1: completa en local, con failover_attempts. Tarea 2: completa en local con UN solo intento, y el remoto
      recibe 0 llamadas.
    - tarea solo-remota con todas las rutas en pausa -> ALL_ROUTES_COOLING_DOWN, retryable True, retry_after > 0
    - target pinned con fallback_allowed False en cooldown -> se intenta igualmente
    - tras expirar el cooldown, el remoto vuelve a ser elegible
    - un fallo local no crea ninguna fila en route_cooldowns
Guía manual F4: pon el base_url de un proveedor cloud a una URL inválida. La primera tarea cae a local; la segunda
no lo intenta; GET /api/v1/models muestra cooldown_until en ese proveedor. Restaura la URL y espera 120 s.

====================================================================================================
FASE 5 — Timeouts de conexión separados de los de lectura
====================================================================================================
5.1 config.py: OllamaConfig.connect_timeout_seconds: float = Field(default=5.0, gt=0);
    DeepSeekConfig.connect_timeout_seconds: float = Field(default=8.0, gt=0);
    OpenAICompatibleProviderConfig.connect_timeout_seconds: float | None = Field(default=None, gt=0).
    Validador: aggregator True y timeout_seconds < 120 -> ValueError("un agregador necesita timeout_seconds >= 120:
    su failover interno consume hasta 45 s antes de responder").
5.2 En las 5 construcciones de httpx.AsyncClient (ollama.py x2, deepseek.py x2, openai_compatible.py x1):
    timeout = httpx.Timeout(config.timeout_seconds, connect=min(connect, config.timeout_seconds)).
    En openai_compatible, connect = connect_timeout_seconds si no es None; si no, 5.0 si deployment == "local"
    y 8.0 en otro caso.
5.3 ANTES de tocar nada: lee la lógica de auto_start de LM Studio. Si depende del timeout del cliente HTTP para
    esperar a que el servidor arranque, PARA y reporta. No lo resuelvas por tu cuenta.
5.4 Tests nuevos: tests/test_provider_timeouts.py
    - client.timeout.connect/read esperados para: Ollama por defecto, DeepSeek por defecto, custom local sin valor,
      custom cloud sin valor, custom con valor explícito, y timeout_seconds menor que connect
    - validador de agregador con timeout bajo
Guía manual F5: base_url de un proveedor cloud a http://10.255.255.1:9/v1. El fallo PROVIDER_UNAVAILABLE con
transport "connect" debe llegar en unos 8 s, no en 300 s. Arranca el broker con LM Studio cerrado y auto_start activo:
debe arrancar y responder como antes.

====================================================================================================
FASE 6 — Mixture: reemplazo de proponentes y cooldown del árbitro
====================================================================================================
6.1 En `_process_consensus`, justo después de obtener proposals/skipped_proposers de round_one y antes del chequeo de
    quórum: si len(proposals) < min(2, len(proposers)) y `_can_replace_proposers(request)`, llama a
    `_replace_failed_proposers(...)` y extiende proposals con lo que devuelva.
    `_can_replace_proposers`: failover.enabled y fallback_allowed y selection.allow_substitution y
    selection.mode != SelectionMode.manual y proposer_skills vacío y agent.mcp_servers vacío.
6.2 `_replace_failed_proposers(repository, task_id, run_id, request, proposers, proposals, skipped, needed)`:
    usa UNA sesión compartida; base_constraints.exclude_models = identidades de TODOS los `proposers`.
    Para cada skipped, en orden y hasta cubrir `needed`:
      role = el role del skipped; ordinal = índice del proponente fallido en `proposers` + 1;
      llama a `_invoke_with_failover(role=role, run_id=run_id, stage="proposing", first_model=None,
        base_constraints=..., call=lambda req, m: self.provider.propose(req, m, ordinal), complete_on_success=True)`
      con request = self._with_remaining_budget(request, [salidas de proposals]);
      si tiene éxito: añade (model, output), suma su identidad a base_constraints y emite el evento
        "proposer.replaced" {"replaced": modelo del skipped, "by": identidad, "role": role};
      si lanza ProviderError: evento "proposer.replacement_failed" y deja de reemplazar.
    Ejecución SECUENCIAL: nunca en paralelo, para no invalidar el plan de VRAM. skipped_proposers se conserva intacto.
6.3 `_synthesize_with_recovery`: tras cada `repository.fail_invocation` del árbitro, calcula classify_failover con una
    sesión desechable y, si procede, registra el cooldown (misma regla que 4.4). No cambies nada más:
    `_substitute_arbiter` y su límite de una sustitución quedan intactos.
6.4 Tests nuevos: tests/test_failover_consensus.py
    - auto, 3 proponentes, 2 fallan y existe un 4.º modelo -> quórum alcanzado con reemplazo
    - quórum alcanzado sin reemplazo -> no hay reemplazo
    - selección manual o proposer_skills -> no hay reemplazo; se mantiene CONSENSUS_QUORUM_NOT_REACHED
    - árbitro remoto con 429 -> cooldown registrado
    - los reemplazos no reutilizan ningún modelo de `proposers`
Guía manual F6: mixture fast con dos proponentes cloud sin saldo y un modelo local disponible. Esperado: completa, con
eventos proposer.replaced.

====================================================================================================
FASE 7 — Map-reduce
====================================================================================================
7.1 `_process_single_map_reduce`: tras calcular `model` y `window`, crea session y
    base = SelectionConstraints(min_context_window=window). Antes de cada fragmento y de cada reducción usa
    `model = session.current_model or model`.
7.2 `_map_reduce_invocation(..., session: FailoverSession | None = None, base_constraints: SelectionConstraints | None = None)`:
    con session, delega en `_invoke_with_failover(first_model=model, role=role, stage="chunking",
    call=lambda _req, m: self.provider.propose(sub_request, m, ordinal), complete_on_success=True)`
    (usa sub_request tal cual). Sin session, el comportamiento es idéntico al actual. Mantén los retornos y la
    comprobación de cancelación.
7.3 `_finalize_map_reduce(..., session=None)`: result["failover_attempts"] si hay fallos.
    Los fragmentos ya procesados NO se repiten.
7.4 Tests nuevos: tests/test_failover_map_reduce.py (patrón de tests/test_long_context.py, con un fake que acepte constraints)
    - 3 fragmentos; el 2.º falla en A -> B procesa el 2.º, el 3.º y la reducción; el 1.º de A se conserva
    - nunca se elige un sustituto con ventana menor que la usada al trocear
    - fallback_allowed False -> falla en el fragmento 2
    Los tests existentes de tests/test_long_context.py pasan sin cambios.
Guía manual F7: documento largo con long_context=map_reduce y el primer modelo cloud sin saldo. Esperado: completa,
con el evento failover.switched en mitad del troceo.

====================================================================================================
FASE 8 — Agente
====================================================================================================
8.1 AgentLoopResult: nuevo campo `model: ModelReference | None = None`, fijado al modelo efectivo en TODOS los returns.
8.2 `_run_agent_loop(..., session: FailoverSession | None = None)`. Solo si session no es None y role == "agent":
    envuelve la invocación del turno en un `while True` interno que NO avanza `step`. Ante un ProviderError de
    agent_turn, tras fail_invocation, llama a `_handle_invocation_failure`:
      - fatal -> decora y relanza
      - retry_same -> duerme retry_same_delay_seconds y repite el turno con el mismo modelo
      - switch -> repite en bucle:
          candidate = `_select_failover_candidate(...)`;
          si self.provider tiene ensure_agent_capable y lanza ProviderError: session.record_failure(candidate, err,
            FailoverDecision("switch", "model", "none", None, "agent_incapable"), ...) SIN fila de invocación, y otro candidato;
          context_window = await self._context_window_for(candidate);
          si not self._fit_agent_conversation(..., context_window, ...): record_failure con code CONTEXT_LIMIT_EXCEEDED
            y exclusión de modelo, y otro candidato;
          si no: model = candidate y se repite el turno con los mismos `messages`.
    Con session None o un role distinto, el comportamiento es idéntico al actual. El turno de cierre no tiene failover.
8.3 `_process_agent`: crea la session y pásala. Usa `loop.model or model` para el resultado, el progreso y model_used.
    Al pausar por tools del cliente, agent_state["model"] = modelo efectivo (model_dump json). Al reanudar, si
    saved_state trae "model" válido, úsalo como `model` SIN llamar a select (ensure_agent_capable sigue ejecutándose).
    Añade result["failover_attempts"] si hay fallos. `invoke_agent` de `_run_proposer_wave` no pasa session.
8.4 README.md: al final de la sección "## 5. Cómo elige los modelos", añade la subsección "### Failover entre modelos"
    con: las dos políticas (remoto y local), el bloque YAML de referencia con los defaults de FailoverConfig, el flag
    aggregator, y los campos failover_attempts y ALL_ROUTES_COOLING_DOWN. Máximo 60 líneas.
8.5 Tests nuevos: tests/test_failover_agent.py
    - el turno 2 falla en remoto A (PROVIDER_UNAVAILABLE) -> B continúa con el mismo historial; la iteración no se
      reinicia; model_used = B
    - un candidato que no pasa ensure_agent_capable se salta sin crear fila de invocación
    - al reanudar tras waiting_for_tools se usa el modelo persistido (select no se llama; verifícalo con un contador)
    - fallback_allowed False -> se propaga el error
    - los proponentes con skills en mixture no hacen failover dentro del bucle
Guía manual F8: agente con web_search cuyo primer modelo cloud está sin saldo. Esperado: completa con otro modelo
con tools, y el detalle muestra el cambio en la iteración correspondiente.

# INFORME DE FASE (formato obligatorio al terminar cada fase)
## Fase N — informe
1. Ficheros creados o modificados (ruta y una línea por fichero)
2. Discrepancias encontradas entre la spec y el código, y cómo se resolvieron (solo tras confirmarlo conmigo; si no hay, "ninguna")
3. Salida resumida de ruff, mypy y pytest (nº de tests, cobertura total y de cada módulo nuevo)
4. Tests nuevos: nombre -> qué demuestra
5. Qué NO se ha podido verificar y cómo verificarlo
6. Guía de prueba manual de la fase (pasos numerados y resultado esperado)
7. Commit: hash y mensaje ("failover F<N>: <resumen>")
Espero "continúa" para empezar la fase N+1.