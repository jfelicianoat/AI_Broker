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

| Campo | Para qué te sirve |
|---|---|
| `contract_version` | `"2.7"`. Si no coincide con lo que esperas, revisa §11 |
| `derived_data_boundary` | `true` → puedes omitir `cloud_allowed` y `allowed_providers` (§4) |
| `work_lanes` | Carriles activos: `["inference"]` o `["inference", "ingestion"]` |
| `strategies` | Qué estrategias acepta. `auto` solo aparece si el meta-router está activo |
| `presets` | Presets válidos **por estrategia** |
| `scheduling_by_preset` | Políticas de planificación válidas por preset |
| `agent_skills` | Skills que el agente puede usar. `run_code` solo si hay sandbox |
| `sandbox_run_code` | Si es `false`, pedir `run_code` da `409` |
| `file_ingestion`, `ingestion_formats` | Si puedes adjuntar ficheros y de qué tipos |
| `long_context_map_reduce` | Si puedes autorizar troceo de documentos largos |
| `max_active_workflows` | Cuántas inferencias corren a la vez (es 1 por invariante) |

**Consúltalo al arrancar tu aplicación, no en cada petición.** Cambia solo cuando cambia la configuración del broker.

---

## 3. Autenticación

Si el broker tiene token admin configurado, **todas** las rutas `/api/v1/*` lo exigen:

```http
X-Admin-Token: <token>
```

Un broker en loopback sin token configurado acepta peticiones sin cabecera. Un broker que escucha fuera de loopback **no arranca** sin token, salvo opt-out explícito. Si recibes `401`/`403`, es esto.

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

El caso contrario sí es terminal: si el modelo pedido no cabe **ni con la máquina vacía**, la tarea falla al momento con `VRAM_MODEL_TOO_LARGE` en vez de esperar un turno que no llegaría nunca.

Ritmo de sondeo recomendado: cada 2–5 s. `progress.phase` e `invocations_completed`/`invocations_total` sirven para pintar avance.

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

Reglas: hasta 16 herramientas, nombres `^[A-Za-z0-9_-]+$` únicos, y **no pueden llamarse igual que una skill integrada**. Debes responder a **todas** las llamadas pendientes en la misma petición o recibes `409`. `waiting_for_tools` no consume el slot de inferencia: la tarea puede esperarte indefinidamente sin bloquear la cola.

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
- **Deja elegir el modelo al broker.** Ordena por tiempo esperado con evidencia de su propia máquina; un `target_model` fijo desactiva eso.
- **Declara la clasificación de datos siempre**, aunque sea `internal`. Es el campo que gobierna a dónde van tus datos, y el default no es una decisión tuya.
- **Usa claves de idempotencia estables** por unidad de trabajo, no UUIDs por intento.
- **Sondea con backoff** y trata cualquier estado no terminal como "sigue trabajando".
- **Pon `max_cost_usd`** si usas modelos de pago: es un corte duro, no un aviso.
- **Respeta `retryable`** del error antes de reintentar.

---

Documentos relacionados: [`../Agent_AI_Broker.md`](../Agent_AI_Broker.md) (guía extensa e histórico de contratos) · [`Phase_9_Speed_And_Lanes.md`](Phase_9_Speed_And_Lanes.md) (por qué el enrutado elige lo que elige) · [`Phase_7_File_Ingestion.md`](Phase_7_File_Ingestion.md) (detalle de la ingesta).
