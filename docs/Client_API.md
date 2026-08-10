# Especificación para aplicaciones cliente

*Contrato 2.7 · 28 de julio de 2026*

Este documento describe **cómo una aplicación envía tareas al broker y recoge el resultado**. Está escrito desde el contrato real (`app/schemas.py`), no desde el histórico de versiones: lo que hay aquí es la forma actual, sin tener que reconstruirla leyendo cinco listas de novedades.

Si vienes del contrato 2.5, salta primero a [§11 Qué ha cambiado](#11-qué-ha-cambiado-desde-25).

---

## 1. Para empezar: el flujo en cuatro pasos

1. **Preguntas qué sabe hacer este broker** (`GET /api/v1/capabilities`). Depende de su configuración: no todos tienen sandbox, ni ingesta de ficheros, ni meta-router.
2. **Envías la tarea** (`POST /api/v1/tasks`). El broker responde `202` inmediatamente con un `task_id`: no espera a tener la respuesta.
3. **Consultas el estado** (`GET /api/v1/tasks/{id}`) cada pocos segundos hasta que el estado sea terminal.
4. **Lees el resultado** del mismo objeto, en `result`.

No hay webhooks ni streaming. El modelo es aceptación asíncrona + sondeo.

```
POST /api/v1/tasks       →  202  { task_id, status: "queued", status_url }
GET  /api/v1/tasks/{id}  →  200  { status: "generating", progress }
GET  /api/v1/tasks/{id}  →  200  { status: "completed", result }
```

---

## 2. Antes de nada: descubrir el broker

```http
GET /api/v1/capabilities
```

Devuelve, entre otros:

| Campo | Forma | Para qué te sirve |
|---|---|---|
| `contract_version` | `string` | `"2.7"`. Si no coincide con lo que esperas, revisa §11 |
| `derived_data_boundary` | `bool` | `true` → puedes omitir `cloud_allowed` y `allowed_providers` (§4) |
| `work_lanes` | `[string]` | Carriles activos: `["inference"]` o `["inference", "ingestion"]` |
| `strategies` | `[string]` | Qué estrategias acepta. `auto` solo aparece si el meta-router está activo |
| `presets` | `{string: [string]}` | Presets válidos **por estrategia**: la clave es la estrategia |
| `scheduling_by_preset` | `{string: [string]}` | Políticas de planificación válidas **por preset**: la clave es el preset |
| `agent_skills` | `[string]` | Skills que el agente puede usar. `run_code` solo si hay sandbox |
| `sandbox_run_code` | `bool` | Si es `false`, pedir `run_code` da `409` |
| `file_ingestion` | `bool` | Si puedes adjuntar ficheros |
| `ingestion_formats` | `{string: [string]}` | Extensiones admitidas **agrupadas por tipo**: la clave es el grupo (`pdf`, `office`, `text`, `image`, `audio`, `video`), el valor su lista de extensiones con punto |
| `long_context_map_reduce` | `bool` | Si puedes autorizar troceo de documentos largos |
| `max_active_workflows` | `int` | Cuántas inferencias corren a la vez (es 1 por invariante) |

**Ojo con los tres campos `{string: [string]}`.** Son **objetos**, no listas. Es el error de integración más común: un cliente con tipado estricto que declara `ingestion_formats` como array falla al deserializar la respuesta entera y se queda sin ninguna capacidad — no solo sin la de ficheros. Si lo que necesitas es la lista plana de extensiones para filtrar un selector de ficheros, aplana tú los valores del mapa.

Respuesta real abreviada, para que no tengas que adivinar la forma:

```json
{
  "contract_version": "2.7",
  "strategies": ["single", "mixture_of_agents", "agent"],
  "presets": { "single": ["fast"], "mixture_of_agents": ["fast", "slow"], "agent": ["fast"] },
  "scheduling_by_preset": { "fast": ["sequential"], "slow": ["adaptive", "parallel", "waves", "sequential"] },
  "agent_skills": ["web_search", "fetch_url", "calculator", "current_datetime", "run_code"],
  "sandbox_run_code": true,
  "file_ingestion": true,
  "ingestion_formats": {
    "pdf": [".pdf"],
    "office": [".docx", ".epub", ".pptx", ".xlsx"],
    "text": [".csv", ".json", ".md", ".py", ".txt"],
    "image": [".jpg", ".png", ".webp"],
    "audio": [".mp3", ".wav"],
    "video": [".mkv", ".mp4"]
  },
  "derived_data_boundary": true,
  "work_lanes": ["inference", "ingestion"]
}
```

**Consúltalo al arrancar tu aplicación, no en cada petición.** Cambia solo cuando cambia la configuración del broker.

**El contrato crece de forma aditiva: no rechaces campos desconocidos.** Entre 2.5 y 2.7 aparecieron `derived_data_boundary`, `work_lanes` y el estado `converting`, y seguirán apareciendo otros. Un cliente que trate un campo nuevo como error se romperá en la siguiente versión del broker sin que nada haya cambiado para él. Ignora lo que no conozcas y da valor por defecto a lo que falte.

**Si no puedes leer `capabilities`, no bloquees al usuario.** La respuesta puede fallar por red, por token o por un campo que tu cliente aún no entiende; ninguna de esas tres cosas significa que el broker no sepa hacer lo que le pides. Avisa, deja enviar la tarea igualmente y confía en el `409` (§11): ese sí distingue un sandbox apagado de un parseo roto. Deducir "no hay sandbox" de un fallo de lectura produce mensajes que apuntan al sitio equivocado y esconden la avería real.

---

## 3. Autenticación

Si el broker tiene token admin configurado, **todas** las rutas `/api/v1/*` lo exigen:

```http
X-Admin-Token: <token>
```

Un broker en loopback sin token configurado acepta peticiones sin cabecera. Un broker que escucha fuera de loopback **no arranca** sin token, salvo opt-out explícito. Si recibes `401`/`403`, es esto.

**El token cambia en cada arranque del broker** salvo que se fije `AI_BROKER_ADMIN_TOKEN` desde fuera. Para una app de larga vida eso significa que un `403` a mitad de trabajo casi nunca es un fallo de integración: es que el broker se ha reiniciado. Trátalo como "hay que renovar credencial y reintentar", no como tarea fallida — **tus tareas siguen ahí**. En particular, una tarea en `waiting_for_tools` sobrevive intacta al reinicio, con su conversación congelada, y te espera. Lo que sí se toca en el arranque son las tareas que estaban ejecutándose: se reencolan, o fallan con `RECOVERY_AMBIGUOUS_REMOTE_CALL` si tenían una llamada remota en vuelo que pudo facturarse.

Distingue `403 ADMIN_AUTH_REQUIRED` (credencial: renuévala) de `503 ADMIN_AUTH_BACKEND_UNAVAILABLE` (el llavero del sistema falla: pedir otro token no arregla nada).

---

## 4. La decisión más importante: la clasificación de datos

Es el único campo de privacidad. Declara qué es el contenido que envías y el broker deriva de ahí a qué modelos puede ir:

```json
{ "risk": { "data_classification": "internal" } }
```

| Valor | ¿Puede salir a la nube? | Proveedores elegibles |
|---|---|---|
| `public` | sí | todos los configurados |
| `internal` | sí | todos los configurados |
| `confidential` | **no** | solo locales |
| `local_only` | **no** | solo locales |

Default si no lo envías: `internal`.

**Esto es una restricción, no una recomendación.** Con `confidential` o `local_only`, un `target_model` que apunte a un proveedor externo hace que la petición falle en validación; no se degrada en silencio a otro modelo.

### Si quieres controlar más fino

Puedes seguir enviando los campos de siempre en `model_requirements`, y mandan sobre la derivación:

```json
{
  "model_requirements": {
    "cloud_allowed": false,
    "allowed_providers": ["ollama", "lmstudio"]
  }
}
```

Con un límite que no se cede: **una clasificación restrictiva no se puede abrir con `cloud_allowed: true`**. Si declaras `confidential`, la tarea se queda en local aunque pidas lo contrario.

Omitirlos es lo normal y lo recomendado: si los envías, tienes dos sitios que mantener coherentes.

---

## 5. Crear una tarea

```http
POST /api/v1/tasks
Content-Type: application/json
```

La petición más pequeña que funciona:

```json
{
  "idempotency_key": "mi-app:informe-42",
  "content": { "prompt": "Resume este texto: ..." }
}
```

Todo lo demás tiene default. Respuesta `202`:

```json
{
  "task_id": "task_9f2c…",
  "status": "queued",
  "execution_strategy": "single",
  "execution_preset": "fast",
  "selection_mode": "auto",
  "status_url": "/api/v1/tasks/task_9f2c…",
  "cancel_url": "/api/v1/tasks/task_9f2c…"
}
```

### 5.1 Campos de primer nivel

| Campo | Tipo | Default | Notas |
|---|---|---|---|
| `idempotency_key` | string **obligatorio** | — | 1–240 caracteres, solo `A-Za-z0-9._:-`. Ver §5.2 |
| `request_id` | string \| null | `null` | Tu identificador de correlación; el broker lo devuelve tal cual |
| `inference_kind` | `chat` \| `embedding` | `chat` | `embedding` exige estrategia `single` y `output.format: "json"` |
| `content` | objeto **obligatorio** | — | §5.3 |
| `output` | objeto | markdown/es | §5.4 |
| `generation` | objeto | 0.3 / 4000 | `temperature` 0–2, `max_output_tokens` ≥ 1 |
| `model_requirements` | objeto | ver §4 y §5.5 | |
| `execution` | objeto | `single`/`fast` | §6 |
| `risk` | objeto | `internal` | §4 |
| `priority` | int 0–1000 | `100` | **Menor valor = antes en la cola** |
| `prompt_compression` | `off` \| `light` \| `medium` \| `aggressive` \| null | `null` | `null` = usar la política global del broker |

La validación es estricta (`extra="forbid"`): **un campo que no exista en el contrato hace fallar la petición con `422`**, no se ignora. Es deliberado — un typo en un nombre de campo es un error silencioso caro.

### 5.2 Idempotencia

`idempotency_key` es tu seguro contra reintentos y contra enviar dos veces lo mismo.

- Mismo `idempotency_key` + **mismo cuerpo** → `200` con la tarea original. No se crea nada nuevo.
- Mismo `idempotency_key` + **cuerpo distinto** → `409 IDEMPOTENCY_CONFLICT`.

La comparación es sobre un hash canónico del cuerpo completo, así que un cambio en cualquier campo cuenta como cuerpo distinto. Usa una clave estable por unidad de trabajo de tu dominio (`"mi-app:factura-42:resumen"`), no un UUID nuevo en cada intento: eso anula la protección.

### 5.3 `content`

| Campo | Tipo | Notas |
|---|---|---|
| `prompt` | string **obligatorio** | Mínimo 1 carácter |
| `attachments` | lista | Solo ficheros ya ingeridos. Ver §7 |
| `metadata` | objeto libre | Se persiste con la tarea; el panel usa `origin` para etiquetar la procedencia |

### 5.4 `output`

| Campo | Valores | Default |
|---|---|---|
| `format` | `markdown` \| `text` \| `json` | `markdown` |
| `json_schema` | objeto | **obligatorio si `format: "json"`** |
| `language` | string 2–16 | `"es"` |

Con `format: "json"` el broker avisa si el modelo elegido tiene verificado por sondeo que **no** soporta salida estructurada.

`language` solo manda donde el broker ya escribe un system prompt propio: `agent` y `mixture_of_agents` (proponentes y árbitro), donde se añade la instrucción de redactar la respuesta final en ese idioma sea cual sea el de la petición o el de las fuentes consultadas. En `single` sigue siendo **metadata inerte**: esa inferencia es transparente por contrato —no recibe system prompt— y el broker no reescribe tu prompt, así que si necesitas fijar el idioma en `single`, dilo en el texto. El valor debe ser una etiqueta de idioma (`es`, `pt-BR`, `zh_Hans`); cualquier otra cosa se ignora entera en vez de sanearse.

### 5.5 `model_requirements`

| Campo | Tipo | Default | Notas |
|---|---|---|---|
| `preferred_model` | string \| null | `null` | Preferencia blanda por nombre de modelo |
| `target_model` | objeto \| null | `null` | Modelo exacto: `{provider, deployment, model}` |
| `fallback_allowed` | bool | `true` | Con `false`, si el modelo exacto no está disponible la tarea **falla** en vez de sustituirlo |
| `cloud_allowed` | bool \| null | `null` (derivado) | §4 |
| `allowed_providers` | lista \| null | `null` (sin restricción) | §4 |
| `max_cost_usd` | float ≥ 0 \| null | `null` | Presupuesto duro: corta la ejecución con `BUDGET_EXCEEDED` |

**Sobre elegir modelo tú mismo:** puedes hacerlo con `target_model`, pero lo normal es no hacerlo. El broker ordena los candidatos por **tiempo esperado hasta una respuesta correcta**, medido en tu propia máquina; fijar un modelo a mano desactiva esa optimización. Úsalo cuando el modelo sea parte del requisito (una comparativa, una reproducción exacta), no por costumbre.

---

## 6. Elegir estrategia

```json
{ "execution": { "strategy": "single", "timeout_seconds": 600 } }
```

| Estrategia | Qué hace | Presets |
|---|---|---|
| `single` | Un modelo responde | `fast` |
| `mixture_of_agents` | Varios proponen, un árbitro sintetiza | `fast`, `slow` |
| `agent` | Bucle de herramientas hasta resolver | `fast` |
| `auto` | El broker clasifica la petición y elige | — |

Campos comunes:

| Campo | Default | Notas |
|---|---|---|
| `timeout_seconds` | `600` | Plazo total de la tarea |
| `long_context` | `"fail"` | Ver §6.4 |
| `scheduling` | `adaptive` | Solo relevante en `mixture_of_agents/slow` |

### 6.1 `single`

Sin más configuración. Es el default y cubre la mayoría de los casos.

### 6.2 `mixture_of_agents`

```json
{
  "execution": {
    "strategy": "mixture_of_agents",
    "preset": "slow",
    "max_proposers": 3,
    "selection": { "mode": "auto", "proposer_count": 3 },
    "proposer_skills": ["web_search", "calculator"]
  }
}
```

- `preset: "fast"` ejecuta los proponentes en serie; `"slow"` los paraleliza según la VRAM disponible.
- `selection.mode`: `auto` (el broker elige), `manual` (exige `proposers` **y** `arbiter` explícitos) o `hybrid`.
- `proposer_skills` da herramientas a los proponentes antes de proponer. **El árbitro nunca usa herramientas.** Solo válido en esta estrategia.
- Se necesitan al menos 2 proponentes con éxito o la tarea falla con `CONSENSUS_QUORUM_NOT_REACHED`.

### 6.3 `agent`

```json
{
  "execution": {
    "strategy": "agent",
    "agent": {
      "skills": ["web_search", "fetch_url"],
      "max_iterations": 6,
      "client_tools": []
    }
  }
}
```

- Skills disponibles: `web_search`, `fetch_url`, `calculator`, `current_datetime`, `run_code`. Confirma cuáles con `capabilities.agent_skills`.
- `max_iterations`: 1–20, default 6.
- **Solo `inference_kind: "chat"` y no admite `output.format: "json"`.**
- Necesita al menos una skill o una `client_tool`.
- `run_code` requiere sandbox activo; si no lo está, la creación falla con `409 SANDBOX_DISABLED`.

Para herramientas propias de tu dominio, ver §9.

### 6.4 Documentos que no caben

Si los adjuntos exceden el contexto de todos los modelos elegibles, el comportamiento por defecto es **fallar explícitamente** con `CONTEXT_LIMIT_EXCEEDED`. El broker nunca trocea ni trunca en silencio.

Puedes autorizar el troceo:

```json
{ "execution": { "strategy": "single", "long_context": "map_reduce" } }
```

Solo con estrategia `single` o `auto`, `inference_kind: "chat"` y sin salida JSON. El resultado incluye `result.long_context` con el número de fragmentos y de invocaciones.

---

## 7. Adjuntar ficheros

Son **tres pasos**, y el segundo no se puede saltar.

### Paso 1 — subir

```http
POST /api/v1/files
Content-Type: multipart/form-data

file=@informe.pdf
```

```json
{
  "file_id": "file_abc123",
  "status": "received",
  "created": true,
  "status_url": "/api/v1/files/file_abc123"
}
```

`created: false` significa que ese fichero ya estaba ingerido (dedupe por SHA-256) y no se vuelve a convertir.

### Paso 2 — esperar a que esté listo

```http
GET /api/v1/files/file_abc123
```

Estados: `received` → `converting` → `ready` | `failed`.

**Sondea hasta `ready`.** Una conversión puede tardar minutos (un PDF escaneado con OCR, un vídeo de una hora). Cuando llega a `ready`, la respuesta incluye `meta.tokens_estimate`: una cota superior de tokens que te sirve para elegir estrategia antes de encolar nada.

### Paso 3 — referenciar en la tarea

```json
{
  "idempotency_key": "mi-app:informe-42",
  "content": {
    "prompt": "Extrae los riesgos del informe adjunto",
    "attachments": [
      { "type": "broker_file", "metadata": { "file_id": "file_abc123" } }
    ]
  }
}
```

También vale `{"type": "broker_file", "uri": "broker://files/file_abc123"}`.

**Solo se admiten adjuntos `broker_file`.** No puedes mandar contenido en línea ni una URL externa: si lo intentas, `422`. Si el fichero aún no está `ready`, la tarea se rechaza con `409 ATTACHED_FILE_NOT_READY` — no se encola para esperar.

Dos comportamientos que conviene conocer:

- Con adjuntos, **la compresión de prompts pasa a `off`** salvo que la pidas explícitamente: comprimir tablas o código de un documento los corrompería.
- Un adjunto **tabular** (`.csv`, `.tsv`, `.xlsx`) no se inyecta en el prompt: llega como manifiesto y se procesa con código. Requiere la skill `run_code`, o la creación falla con `409 TABULAR_ATTACHMENT_REQUIRES_SANDBOX`.

---

## 8. Consultar el estado y leer el resultado

```http
GET /api/v1/tasks/{task_id}
```

```json
{
  "task_id": "task_9f2c…",
  "kind": "inference",
  "status": "completed",
  "request_id": "mi-correlacion-42",
  "created_at": "2026-07-26T10:00:00+00:00",
  "updated_at": "2026-07-26T10:00:07+00:00",
  "execution_strategy": "single",
  "execution_preset": "fast",
  "selection_mode": "auto",
  "progress": { "phase": "completed", "invocations_completed": 1, "invocations_total": 1 },
  "result": { "…": "…" },
  "error": null
}
```

### Estados

| Grupo | Estados |
|---|---|
| En espera | `queued`, `waiting_for_memory` |
| En curso (inferencia) | `routing`, `planning`, `resource_planning`, `chunking`, `generating`, `proposing`, `evaluating`, `debating`, `synthesizing`, `verifying` |
| En curso (conversión) | `converting` |
| Pausada | `waiting_for_tools` (ver §9) |
| Terminales | `completed`, `failed`, `cancelled` |

**No hagas lógica sobre los estados intermedios concretos**: son etapas técnicas y pueden cambiar. Trata cualquier estado no terminal como "sigue trabajando". Los terminales sí son contrato.

`waiting_for_memory` (contrato 2.7) merece una nota porque **no es un fallo y no requiere que hagas nada**: la máquina no tiene memoria libre ahora mismo, así que la tarea ha cedido el turno conservando su sitio en la cola y volverá sola. Mientras espera, el broker adelanta a las tareas que sí quepan. Sigue sondeando igual que en `queued`. Si quieres explicarlo en tu interfaz, `GET /api/v1/queue` trae en esa tarea qué modelo pedía y quién ocupa la memoria. La espera no caduca: si algo ajeno al broker retiene la memoria para siempre, la tarea seguirá esperando hasta que la canceles con `POST /api/v1/tasks/{id}/cancel`.

El caso contrario sí es terminal: si el modelo pedido **pesa más que todo el presupuesto de memoria local configurado** (menos el margen de seguridad), la tarea falla al momento con `VRAM_MODEL_TOO_LARGE` en vez de esperar un turno que no llegaría nunca. Ojo con lo que este error no dice: no habla de la memoria libre real ni de otros procesos, solo compara el peso del modelo con el presupuesto.

Cuál es ese presupuesto depende de la máquina donde corra el broker. En GPU discreta es `resources.local_vram_budget_gb`, y un modelo mayor que la VRAM es terminal aunque Ollama supiera repartirlo a RAM. En máquinas de memoria unificada (APU, Apple Silicon) el operador declara `resources.unified_memory_budget_gb` con el pool completo, y entonces manda ese: ahí la VRAM es una porción de la misma RAM física, así que un modelo mayor que la VRAM se reparte y sigue siendo ejecutable. El mensaje de error dice siempre cuál de los dos techos ha cortado.

Ritmo de sondeo recomendado: cada 2–5 s. `progress.phase` e `invocations_completed`/`invocations_total` sirven para pintar avance.

### El esquema de esta respuesta

`progress` y `result` son objetos abiertos, así que conviene decir qué parte es promesa y qué parte es información. El **núcleo garantizado** está publicado como JSON Schema en `tests/fixtures/broker_task_state_response.schema.json`, y un test de contrato valida contra él las respuestas reales del endpoint: no puede quedarse obsoleto en silencio. Cópialo a tu repo si quieres validar en tu lado.

Lo que fija:

- Siempre: `task_id`, `kind`, `status`, `request_id`, `created_at`, `updated_at`, `progress.phase`.
- Con `kind: "inference"`: además `progress.invocations_completed` y `progress.invocations_total`.
- Con `status: "waiting_for_tools"`: `result.pending_tool_calls`, cada una con `id`, `name` y `arguments` (ver §9); y en la estrategia `agent`, `progress.agent_iteration` y `progress.agent_max_iterations`.
- Con `status: "failed"`: `error` con `code`, `message` y `retryable`.

**Cualquier otra clave de `progress` es informativa** —existe, puede ser útil para pintar, y puede cambiar o desaparecer sin subir versión de contrato—. Los contadores son un **agregado**: te dicen cuántas invocaciones van, no cuáles. El detalle por invocación del broker (modelo, coste, latencia, llamadas a skills) vive en el panel de operación, `GET /api/v1/dashboard/tasks/{id}`, que es un contrato de administración y no de cliente. Si lo que quieres es el detalle de *tus* pasos, la vía es §9.

### El resultado

Para `chat`:

| Campo | Contenido |
|---|---|
| `assistant_content` | **La respuesta.** Es lo que buscas |
| `result_markdown` | Igual que el anterior (alias histórico) |
| `model_used` | `{provider, deployment, model}` que respondió |
| `models_used` | Todos los que intervinieron |
| `fallback_used` | `true` si no se pudo usar el modelo exacto pedido |
| `usage` | Tokens y coste reales |
| `inference_kind`, `output_format` | Eco de lo que pediste |

Para `embedding`: `result.embedding` con el vector.

Según la estrategia aparecen además `result.agent` (iteraciones, skills usadas), `result.long_context` (fragmentos) o el detalle del consenso.

### Cuando falla

`status: "failed"` y `error` con `{code, message, retryable}`. **`retryable` te dice si tiene sentido reintentar**: un `PROVIDER_UNAVAILABLE` transitorio sí, un `CONTEXT_LIMIT_EXCEEDED` no.

### El campo `kind`

`inference` para tus tareas. `ingestion` aparece si consultas tareas de conversión de ficheros, que el broker también expone como tareas; en esas, `execution_strategy`, `execution_preset` y `selection_mode` son `null` — una conversión no tiene ejecución que describir. Si tu cliente asume que esos campos siempre traen valor, míralo antes.

---

## 9. Herramientas propias (passthrough)

Si el modelo necesita algo que solo tu aplicación sabe hacer —consultar tu base de datos, crear un ticket—, decláralo como `client_tool`. El broker **ofrece** la herramienta al modelo pero **no la ejecuta**: pausa la tarea y te devuelve la llamada.

```json
{
  "execution": {
    "strategy": "agent",
    "agent": {
      "skills": [],
      "client_tools": [
        {
          "name": "consultar_stock",
          "description": "Devuelve unidades disponibles de un artículo",
          "parameters": {
            "type": "object",
            "properties": { "sku": { "type": "string" } },
            "required": ["sku"]
          }
        }
      ]
    }
  }
}
```

Flujo:

1. La tarea pasa a `waiting_for_tools` y `result.pending_tool_calls` trae las llamadas con su `tool_call_id`.
2. Las ejecutas tú.
3. Devuelves los resultados:

```http
POST /api/v1/tasks/{task_id}/tool_results
{
  "tool_results": [
    { "tool_call_id": "call_1", "content": "42 unidades" }
  ]
}
```

4. La tarea vuelve a `queued` y el bucle continúa donde estaba.

Reglas: hasta 16 herramientas, nombres `^[A-Za-z0-9_-]+$` únicos, y **no pueden llamarse igual que una skill habilitada en esa misma tarea** — dos definiciones del mismo nombre en una llamada al modelo son ambiguas. Con `skills: []` la lista de nombres queda libre: puedes llamar `web_search` a tu propia búsqueda, que es el nombre con el que los modelos vienen entrenados. Debes responder a **todas** las llamadas pendientes en la misma petición o recibes `409`. `waiting_for_tools` no consume el slot de inferencia: la tarea puede esperarte indefinidamente sin bloquear la cola.

### Cuando la orquestación es tuya

Si tu aplicación quiere conservar el control del bucle —decidir qué se busca, con qué fuentes y cuándo parar— declara **todas** las herramientas como `client_tools` y deja `skills: []`. El broker aporta el razonamiento del modelo y tú ejecutas cada paso, así que ves cada subtarea con su nombre y sus argumentos en `result.pending_tool_calls` antes de resolverla. Es el punto medio entre pedir una inferencia suelta por paso (control total, sin agente) y entregar el bucle entero al broker con sus skills integradas (sin visibilidad del detalle: solo el agregado de `progress`).

`progress.agent_iteration` y `progress.agent_max_iterations` te dicen por qué vuelta del bucle vas. Ojo: **el tope de `max_iterations` cuenta el bucle entero**, pausas incluidas — al reanudar se sigue contando donde se quedó, no se reinicia. Con un máximo de 20, esa es la profundidad total de la investigación.

Reanudar **no te manda al final de la cola**: la tarea vuelve al sitio que le da su hora de llegada, por delante de lo que entró mientras tú ejecutabas la herramienta. Pausar para pedirte algo no degrada tu tarea, igual que no la degrada esperar memoria.

---

## 10. Cancelar

```http
DELETE /api/v1/tasks/{task_id}
```

Idempotente: cancelar algo ya terminal devuelve su estado sin error. Una tarea en curso se marca cancelada y se interrumpe entre invocaciones.

---

## 11. Errores

| HTTP | Código | Qué ha pasado | Qué hacer |
|---|---|---|---|
| `422` | `CONTRACT_VALIDATION_FAILED` | El cuerpo no cumple el contrato. Trae `fields` con las rutas exactas | Corregir. No reintentar igual |
| `409` | `IDEMPOTENCY_CONFLICT` | Misma clave, cuerpo distinto | Usar otra clave, o reenviar el cuerpo original |
| `409` | `ATTACHED_FILE_NOT_READY` | Adjunto aún convirtiéndose | Sondear `/files/{id}` y reintentar |
| `404` | `ATTACHED_FILE_NOT_FOUND` | El `file_id` no existe | Volver a subir |
| `409` | `INGESTION_DISABLED` | Este broker no acepta adjuntos | Comprobar `capabilities.file_ingestion` |
| `409` | `SANDBOX_DISABLED` | Pediste `run_code` sin sandbox | Comprobar `capabilities.sandbox_run_code` |
| `409` | `TABULAR_ATTACHMENT_REQUIRES_SANDBOX` | CSV/XLSX sin `run_code` | Usar `agent` o `mixture_of_agents` con `run_code` |
| `429` | `QUEUE_FULL` | Cola llena | Backoff y reintentar |
| `401`/`403` | — | Falta o no vale el token admin | Revisar `X-Admin-Token` |

Errores **durante la ejecución** no son HTTP: la petición ya se aceptó con `202` y el fallo llega en `error` del estado de la tarea. Los que verás:

| Código | Significa | `retryable` |
|---|---|---|
| `MODEL_UNAVAILABLE` | No hay modelo elegible, o el exacto pedido no está | no |
| `CONTEXT_LIMIT_EXCEEDED` | La petición no cabe en el contexto | no — acorta o autoriza `map_reduce` |
| `CONTEXT_WINDOW_UNKNOWN` | El modelo exacto no declara su contexto | no |
| `BUDGET_EXCEEDED` | Se agotó `max_cost_usd` a mitad | no — sube el presupuesto |
| `CONSENSUS_QUORUM_NOT_REACHED` | Menos de 2 proponentes con éxito | según causa |
| `PROVIDER_UNAVAILABLE` | Proveedor caído o apagado | **sí** |
| `PROVIDER_NOT_ALLOWED` / `CLOUD_NOT_ALLOWED` | La frontera de datos bloqueó el modelo | no — revisa §4 |
| `MODEL_CAPABILITY_MISMATCH` | El modelo no hace lo que pide la tarea (visión, JSON, tools) | no |
| `TASK_TIMEOUT` | Se agotó `execution.timeout_seconds` | según causa |
| `ATTACHMENT_EXPANSION_FAILED` | No se pudo inyectar el documento adjunto | no |
| `TASK_RETRY_LIMIT_EXCEEDED` | La tarea se reintentó tras varios reinicios del broker | no |

Guíate por el campo `retryable` del error, no por la tabla: es el broker quien sabe si aquel fallo concreto fue transitorio.

---

## 12. Qué ha cambiado desde 2.5

Si tu cliente ya funcionaba, **lo más probable es que siga funcionando sin tocar nada**. Los cambios son aditivos o relajaciones, con una excepción de comportamiento.

### Puede que quieras simplificar

`cloud_allowed` y `allowed_providers` pasan de obligatoriamente-explícitos a derivados. Antes, para que un modelo cloud fuese candidato tenías que acertar **tres** campos a la vez, y dos de ellos tenían defaults restrictivos (`false` y `["ollama"]`). Si tu cliente solo declaraba la clasificación de datos, se quedaba en local sin saberlo.

Ahora basta con `risk.data_classification`. Puedes borrar los otros dos de tus peticiones.

### Un cambio de comportamiento que debes revisar

**`confidential` ahora bloquea el cloud.** Antes solo marcaba la tarea como sensible para el meta-router y podía salir a la nube igual que una `internal`. Si tu aplicación usaba `confidential` esperando que la tarea saliera a un modelo externo, ahora se quedará en local.

Es un endurecimiento deliberado: el nombre prometía una frontera que no se aplicaba.

### Novedades que puedes ignorar si no las necesitas

- `TaskStateResponse` trae `kind`; `execution_strategy`, `execution_preset` y `selection_mode` pueden ser `null` (solo en tareas de tipo `ingestion`).
- Estado nuevo `converting`, solo en el carril de conversiones.
- `capabilities` trae `derived_data_boundary` y `work_lanes`.
- `GET /api/v1/dashboard/tasks?kind=` filtra por carril.

---

## 13. Ejemplos completos

### Resumen sencillo, sin salir de la máquina

```json
{
  "idempotency_key": "mi-app:acta-2026-07-26",
  "content": { "prompt": "Resume esta acta en cinco puntos:\n\n…" },
  "risk": { "data_classification": "confidential" }
}
```

### Extracción estructurada de un PDF, priorizando velocidad

```json
{
  "idempotency_key": "mi-app:factura-8891",
  "content": {
    "prompt": "Extrae los campos de la factura adjunta",
    "attachments": [
      { "type": "broker_file", "metadata": { "file_id": "file_abc123" } }
    ]
  },
  "output": {
    "format": "json",
    "json_schema": {
      "type": "object",
      "properties": {
        "numero": { "type": "string" },
        "total": { "type": "number" }
      },
      "required": ["numero", "total"]
    }
  },
  "risk": { "data_classification": "internal" },
  "model_requirements": { "max_cost_usd": 0.10 }
}
```

### Análisis deliberado con presupuesto y troceo autorizado

```json
{
  "idempotency_key": "mi-app:auditoria-q3",
  "content": {
    "prompt": "Compara los tres informes adjuntos y señala contradicciones",
    "attachments": [
      { "type": "broker_file", "metadata": { "file_id": "file_a" } },
      { "type": "broker_file", "metadata": { "file_id": "file_b" } },
      { "type": "broker_file", "metadata": { "file_id": "file_c" } }
    ]
  },
  "execution": {
    "strategy": "auto",
    "long_context": "map_reduce",
    "timeout_seconds": 1800
  },
  "model_requirements": { "max_cost_usd": 1.0 },
  "risk": { "data_classification": "internal" }
}
```

### Agente con herramienta propia

```json
{
  "idempotency_key": "mi-app:pedido-551",
  "content": { "prompt": "¿Podemos servir el pedido 551 esta semana?" },
  "execution": {
    "strategy": "agent",
    "agent": {
      "skills": ["current_datetime"],
      "max_iterations": 8,
      "client_tools": [
        {
          "name": "consultar_pedido",
          "description": "Devuelve líneas y stock de un pedido",
          "parameters": {
            "type": "object",
            "properties": { "id": { "type": "integer" } },
            "required": ["id"]
          }
        }
      ]
    }
  }
}
```

---

## 14. Recomendaciones de integración

- **Consulta `capabilities` al arrancar** y adapta lo que ofreces a tu usuario. Pedir `run_code` a un broker sin sandbox es un `409` evitable.
- **Deserializa `capabilities` con tolerancia** (§2): `presets`, `scheduling_by_preset` e `ingestion_formats` son objetos, no listas; los campos que no conozcas se ignoran, los que falten van por defecto. Y si aun así no puedes leerlo, avisa pero deja enviar: un fallo de lectura no es una capacidad ausente.
- **Deja elegir el modelo al broker.** Ordena por tiempo esperado con evidencia de su propia máquina; un `target_model` fijo desactiva eso.
- **Declara la clasificación de datos siempre**, aunque sea `internal`. Es el campo que gobierna a dónde van tus datos, y el default no es una decisión tuya.
- **Usa claves de idempotencia estables** por unidad de trabajo, no UUIDs por intento.
- **Sondea con backoff** y trata cualquier estado no terminal como "sigue trabajando".
- **Pon `max_cost_usd`** si usas modelos de pago: es un corte duro, no un aviso.
- **Respeta `retryable`** del error antes de reintentar.

---

Documentos relacionados: [`../Agent_AI_Broker.md`](../Agent_AI_Broker.md) (guía extensa e histórico de contratos) · [`Phase_9_Speed_And_Lanes.md`](Phase_9_Speed_And_Lanes.md) (por qué el enrutado elige lo que elige) · [`Phase_7_File_Ingestion.md`](Phase_7_File_Ingestion.md) (detalle de la ingesta).
