# Especificación para aplicaciones cliente

*Contrato 2.9 · 16 de agosto de 2026*

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
| `contract_version` | `string` | `"2.9"`. Si no coincide con lo que esperas, revisa §12 |
| `derived_data_boundary` | `bool` | `true` → puedes omitir `cloud_allowed` y `allowed_providers` (§4) |
| `work_lanes` | `[string]` | Carriles activos: `["inference"]` o `["inference", "ingestion"]` |
| `strategies` | `[string]` | Qué estrategias acepta. `auto` solo aparece si el meta-router está activo |
| `presets` | `{string: [string]}` | Presets válidos **por estrategia**: la clave es la estrategia |
| `scheduling_by_preset` | `{string: [string]}` | Políticas de planificación válidas **por preset**: la clave es el preset |
| `agent_skills` | `[string]` | Skills que el agente puede usar. `run_code` solo si hay sandbox |
| `agent_skills_egress` | `[string]` | Subconjunto que saca contenido de la máquina. Prohibidas bajo frontera local (§4) |
| `task_dependencies` | `bool` | Si puedes usar `group`, `depends_on` y `depends_on_group` (§5.6) |
| `mcp_servers` | `[{id, data_boundary}]` | Servidores MCP disponibles para `execution.agent.mcp_servers`. Vacío = MCP apagado |
| `sandbox_run_code` | `bool` | Si es `false`, pedir `run_code` da `409` |
| `file_ingestion` | `bool` | Si puedes adjuntar ficheros |
| `ingestion_formats` | `{string: [string]}` | Extensiones admitidas **agrupadas por tipo**: la clave es el grupo (`pdf`, `office`, `text`, `image`, `audio`, `video`), el valor su lista de extensiones con punto |
| `long_context_map_reduce` | `bool` | Si puedes autorizar troceo de documentos largos |
| `max_active_workflows` | `int` | Cuántas inferencias corren a la vez (es 1 por invariante) |

**Ojo con los tres campos `{string: [string]}`.** Son **objetos**, no listas. Es el error de integración más común: un cliente con tipado estricto que declara `ingestion_formats` como array falla al deserializar la respuesta entera y se queda sin ninguna capacidad — no solo sin la de ficheros. Si lo que necesitas es la lista plana de extensiones para filtrar un selector de ficheros, aplana tú los valores del mapa.

Respuesta real abreviada, para que no tengas que adivinar la forma:

```json
{
  "contract_version": "2.9",
  "strategies": ["single", "mixture_of_agents", "agent"],
  "presets": { "single": ["fast"], "mixture_of_agents": ["fast", "slow"], "agent": ["fast"] },
  "scheduling_by_preset": { "fast": ["sequential"], "slow": ["adaptive", "parallel", "waves", "sequential"] },
  "agent_skills": ["web_search", "fetch_url", "calculator", "current_datetime", "run_code"],
  "agent_skills_egress": ["fetch_url", "web_search"],
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
  "task_group_status": true,
  "work_lanes": ["inference", "ingestion"],
  "generation_determinism": true,
  "exclude_from_model_learning": true,
  "invocation_telemetry": true,
  "execution_fingerprint": true
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

| Valor | ¿Puede salir a la nube? | Proveedores elegibles | Skills con salida a red |
|---|---|---|---|
| `public` | sí | todos los configurados | sí |
| `internal` | sí | todos los configurados | sí |
| `confidential` | **no** | solo locales | **no** |
| `local_only` | **no** | solo locales | **no** |

Default si no lo envías: `internal`.

**Esto es una restricción, no una recomendación.** Con `confidential` o `local_only`, un `target_model` que apunte a un proveedor externo hace que la petición falle en validación; no se degrada en silencio a otro modelo.

**La frontera cubre las herramientas, no solo los modelos.** De poco sirve elegir un modelo local si el agente manda tu consulta a un buscador: bajo frontera local, `web_search` y `fetch_url` se rechazan igual que un proveedor cloud (§6.3). La lista exacta está en `capabilities.agent_skills_egress`.

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
| `generation` | objeto | 0.3 / 4000 | `temperature` 0–2, `max_output_tokens` ≥ 1, `seed`, `top_p`. Ver §5.7 |
| `model_requirements` | objeto | ver §4 y §5.5 | |
| `execution` | objeto | `single`/`fast` | §6 |
| `risk` | objeto | `internal` | §4 |
| `priority` | int 0–1000 | `100` | **Menor valor = antes en la cola** |
| `prompt_compression` | `off` \| `light` \| `medium` \| `aggressive` \| null | `null` | `null` = usar la política global del broker |
| `group` | string \| null | `null` | Etiqueta de esta tarea, para que otras puedan esperarla en bloque. Ver §5.6 |
| `depends_on` | lista de task_id | `[]` | Tareas que deben terminar antes que esta. Máximo 64 |
| `depends_on_group` | string \| null | `null` | Grupo que debe estar drenado antes que esta |
| `exclude_from_model_learning` | bool | `false` | Esta tarea no alimenta las métricas del router. Ver §5.8 |

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

`language` solo manda donde el broker ya escribe un system prompt propio: `agent` y `mixture_of_agents` (proponentes y árbitro), donde se añade la instrucción de redactar la respuesta final en ese idioma sea cual sea el de la petición o el de las fuentes consultadas. En `single` sigue siendo **metadata inerte**: esa inferencia es transparente por contrato —no recibe system prompt— y el broker no reescribe tu prompt, así que si necesitas fijar el idioma en `single`, dilo en el texto. El valor debe ser una etiqueta de idioma (`es`, `pt-BR`, `zh_Hans`); cualquier otra cosa se ignora entera en vez de sanearse. El texto literal de cada system prompt que el broker escribe está en [`System_Prompts.md`](System_Prompts.md).

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

### 5.6 Dependencias entre tareas

El caso que las motiva: subes un documento, se indexa en cientos de tareas de embedding, y la pregunta sobre ese documento **no debe correr hasta que el índice esté entero**. Si la envías antes, se responde con un índice a medias y la respuesta parece buena.

Dos formas de declararlo, combinables:

```json
{ "depends_on": ["task_abc", "task_def"] }
```

```json
{ "group": "idx-doc7" }        // en cada tarea de indexado
{ "depends_on_group": "idx-doc7" }   // en la pregunta
```

El grupo existe porque enviar los cientos de identificadores en cada pregunta no es viable. Una tarea **no puede** depender de su propio grupo (`422`): sería esperarse a sí misma, y las tareas del índice se bloquearían entre ellas.

**Mientras espera**, la tarea queda en `waiting_for_dependencies`. Es hermano de `waiting_for_memory`: no es un fallo, **no consume el workflow único** —el resto de la cola sigue avanzando, que es lo que permite que su propia dependencia corra— y no pierde su sitio en la cola. El panel muestra a qué espera.

**Reglas que conviene conocer antes de usarlo:**

| Situación | Qué hace el broker |
|---|---|
| La dependencia termina `failed` o `cancelled` | La considera resuelta y **sigue adelante**, con un aviso en `result.warnings`. Esperar eternamente a algo que ya no va a terminar sería peor |
| El `task_id` no existe | No bloquea (no se puede esperar a algo que no está) y lo dice en `result.warnings` |
| La espera supera `execution.timeout_seconds` | **Se ejecuta igual**, con un aviso en `result.warnings` y un evento `task.dependency_timeout` |
| Se crean tareas del grupo *después* de que la que espera haya sido reclamada | No las espera: el grupo se evalúa en cada reclamo sobre lo que existe en ese momento |

Es decir: **la dependencia ordena, no condiciona**. En ningún caso una dependencia incumplida hace fallar tu tarea; siempre acaba ejecutándose. Si para tu caso una dependencia rota debe impedir la respuesta, tienes que mirar `result.warnings` y decidir tú — por eso el aviso viaja en el resultado y no solo en los eventos.

Un grupo que no drena nunca no cuelga la cola: cada tarea que espera tiene su propio tope y acaba pasando.

---

### 5.7 Reproducibilidad: `seed` y `top_p`

`generation` admite dos campos más, ambos opcionales:

| Campo | Tipo | Default | Notas |
|---|---|---|---|
| `seed` | int ≥ 0 \| null | `null` | Semilla de muestreo |
| `top_p` | float 0–1 \| null | `null` | Muestreo por núcleo. Fuera de rango → `CONTRACT_VALIDATION_FAILED` |

**`null` significa «no enviar el campo»**, no «enviar el valor por defecto del proveedor». Una petición sin `seed` produce exactamente el mismo cuerpo hacia el modelo que antes de que estos campos existieran: si no los usas, no cambia nada para ti.

**El broker no promete que la semilla se aplique.** Que un modelo la honre depende de su runtime, y eso no es observable desde fuera. Lo que sí se declara es qué se envió, por invocación (§8.1):

| `seed_status` | Significa |
|---|---|
| `not_requested` | No pediste semilla |
| `sent` | El broker la incluyó en la petición al proveedor |
| `unsupported` | Ese endpoint no tiene por dónde llevarla (p. ej. `embeddings`); pedirla no cambió nada |

Si mides reproducibilidad, **mira `seed_status` antes de dar por buena una comparación**: una semilla aceptada en silencio y descartada por debajo es peor que no tenerla.

### 5.8 Tráfico que no debe enseñar al broker

`exclude_from_model_learning: true` marca una tarea como **no representativa**.

El problema que resuelve: el broker aprende de lo que pasa. Cada invocación mueve la tasa de fiabilidad del modelo, su latencia media y su cercanía a la cuarentena. Si sometes a un modelo a prompts deliberadamente difíciles —contexto extremo, instrucciones contradictorias, entradas adversariales— le estás bajando la nota al modelo en producción por hacer justo lo que esperabas que hiciera. Con este marcador, **medir un modelo deja de degradarlo**.

Qué hace exactamente:

- **No** cuenta para `model_stats`, la cuarentena, la estimación de tiempo ni la elección de aspirante del sondeo en sombra.
- **Sí** consume presupuesto, cuota, semáforo y VRAM como cualquier otra tarea, y **sí** aparece en coste, uso y panel. Está excluida del aprendizaje, no de la contabilidad.

Es un campo de primer nivel a propósito, y no una etiqueta dentro de `content.metadata`: esa metadata es opaca por contrato, y hacer que el router dependa de una cadena libre sería un acoplamiento que nadie podría sospechar leyendo el código. Anunciado en `capabilities.exclude_from_model_learning`.

---

### 5.9 Lanzar una tanda y seguirla

Si vas a mandar decenas o cientos de tareas —una evaluación, un índice—, **mándalas todas de golpe con el mismo `group`** y deja que la cola durable las ordene. No esperes a que termine una para enviar la siguiente: el broker ejecuta una cada vez de todos modos (es su invariante), así que esperar no protege la máquina, solo desaprovecha la cola y deja el panel vacío mientras trabajas.

Para seguirla no hace falta sondear N identificadores:

```http
GET /api/v1/groups/{group}
```

| Campo | Contenido |
|---|---|
| `total` | Tareas de la tirada |
| `pending` | En cola o esperando (memoria, dependencias, herramientas) |
| `active` | Ejecutándose ahora |
| `completed`, `failed`, `cancelled` | Cómo acabaron |
| `finished` | `true` cuando ninguna sigue viva. **Es el campo que hay que sondear** |
| `created_at`, `updated_at` | Cuándo empezó la tirada y cuándo se movió por última vez |

`finished: true` significa terminada, **no** exitosa: una tirada con fallos también termina. Mira `failed` para eso.

Un grupo sin ninguna tarea devuelve `404 GROUP_NOT_FOUND` en vez de ceros, para que un nombre mal escrito no parezca una tirada ya acabada. Anunciado en `capabilities.task_group_status`.

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
- `selection.arbiter_policy`: `strongest_available` (por defecto) elige el árbitro por tamaño declarado, no por rapidez — el árbitro juzga, no responde. Incluye modelos cloud si la tarea los permite, así que puede no ser local. `fastest_available` lo elige con el mismo ranking por tiempo que a los proponentes. Cualquier otro valor se **rechaza** en la validación.
- `proposer_skills` da herramientas a los proponentes antes de proponer. **El árbitro nunca usa herramientas.** Solo válido en esta estrategia.
- Se necesitan al menos 2 proponentes con éxito o la tarea falla con `CONSENSUS_QUORUM_NOT_REACHED`.
- Si falla el árbitro, el broker prueba otro y si tampoco puede entrega la mejor propuesta (ver «El resultado»). Con `selection.arbiter`, `preferred_arbiter` o `allow_substitution: false` no se cambia de árbitro: solo se degrada.

**Segunda ronda (`max_rounds`).** Con `max_rounds: 2` los proponentes pueden ver la síntesis de la primera ronda y mejorarla, y el árbitro sintetiza otra vez sobre lo refinado. Es un **techo, no una orden**, igual que `max_proposers`:

| `max_judges` | Qué pasa con `max_rounds: 2` |
|---|---|
| `1` (por defecto) | Un juez puntúa la síntesis de la ronda 1. Solo se paga la segunda si baja del umbral. Coste típico: una invocación extra si la primera ronda ya era buena |
| `0` | Sin juez: la segunda ronda se gasta siempre. Coste: el doble de proponentes más otro árbitro |

**La segunda ronda no puede dejarte peor que la primera.** Si no queda presupuesto, no hay quorum, el árbitro cae o el contexto se desborda con la síntesis añadida, recibes la síntesis de la ronda 1 y la tarea termina `completed`. No hay ningún caso en que pedir dos rondas convierta en fallo una tarea que con una habría funcionado.

En el resultado, `consensus.rounds` te dice cuántas se ejecutaron de verdad y `consensus.confidence` la puntuación del juez (`null` si no lo hubo). El coste de la ronda 1 sigue contando en `usage` aunque su respuesta no sea la que recibes: se pagó.

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
- **Frontera de datos.** `web_search` y `fetch_url` sacan contenido de la tarea de esta máquina; el resto se resuelve dentro (el sandbox de `run_code` corre con `--network none`). Con `risk.data_classification` en `confidential` o `local_only`, pedirlas explícitamente —en `agent.skills` o en `proposer_skills`— falla la creación con `422`, igual que pedir un `target_model` cloud: da igual que el modelo sea local si el texto de la tarea sale igual. Si **no** eliges skills, la lista por defecto se acota sola a las locales y la tarea sigue adelante: un default del broker no debe hacer fallar tu petición. La lista exacta está en `capabilities.agent_skills_egress`.
- Con `strategy: "auto"` y frontera local, el meta-router no elige `agent` por señales de red (`needs_recent`, `has_url`): sin skills de red esa estrategia no puede cumplir su motivo. Sí lo sigue eligiendo por señal de cálculo, que se resuelve en la máquina.
- **Herramientas de servidores MCP**: `agent.mcp_servers: ["<id>"]` añade las herramientas de un servidor MCP configurado en el broker, con nombres `mcp__<servidor>__<tool>`. Los ids disponibles y su frontera de datos están en `capabilities.mcp_servers`; pedir uno inexistente, con MCP apagado, o uno `egress` bajo frontera local da `409`. Detalle en [`MCP_Servers.md`](MCP_Servers.md).
- `max_iterations`: 1–20, default 6. Acota las rondas **con** herramientas. Al agotarlas, el broker pide una última respuesta **sin** herramientas en vez de devolverte un mensaje de error con el trabajo hecho tirado: la tarea termina `completed`, con `result.agent.stop_reason: "max_iterations"` y `result.agent.final_turn: true`. Ese turno de cierre no suma en `result.agent.iterations` (que sigue respetando el tope) pero sí en `result.usage.invocations` y en el coste, porque es una invocación real. Si el turno de cierre también falla, recibes el mensaje de iteraciones agotadas y `final_turn: false`.
- Agotar `max_cost_usd` a media investigación no dispara turno de cierre —costaría por encima de un techo que tú fijaste como duro—: se te entrega lo último que llegó a decir el modelo, si dijo algo, seguido del aviso de presupuesto (`stop_reason: "budget_exhausted"`).
- La conversación del bucle se mide contra la ventana del modelo antes de cada vuelta y los resultados de herramienta más antiguos se recortan para que quepa. Si aun así no cabe, `stop_reason: "context_exhausted"` con lo último que dijo el modelo. Un `max_iterations` alto con `fetch_url` es la receta para llegar ahí: cada página descargada ocupa contexto que ya no se recupera.
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
describe_images=false        # opcional
```

```json
{
  "file_id": "file_abc123",
  "status": "received",
  "created": true,
  "status_url": "/api/v1/files/file_abc123",
  "describe_images": false
}
```

`created: false` significa que ese fichero ya estaba ingerido (dedupe por SHA-256) y no se vuelve a convertir.

#### `describe_images` — el interruptor de las figuras

Decide si las imágenes del documento se extraen y se describen con un modelo de visión para que su contenido acabe en el Markdown.

| valor | efecto |
|---|---|
| omitido | hereda `ingestion.images.enabled` del broker |
| `true` | describe las figuras |
| `false` | las ignora: la conversión va **mucho** más rápida |

Es la parte cara de la ingesta —una llamada a un modelo por figura, además del renderizado de cada imagen—, y en un documento grande puede tardar más que todo lo demás junto. Cuando las imágenes son sellos, logotipos o decoración, `false` no pierde nada de información útil.

La respuesta y el estado del fichero devuelven siempre la política **ya resuelta**, así que sabes qué vas a recibir sin consultar la configuración del broker. En el Markdown de un fichero convertido sin descripciones, `meta.images_described` vale `false`: un documento sin descripciones no es lo mismo que un documento sin figuras.

**Efecto sobre la deduplicación**: la política forma parte de la identidad de la conversión. Una conversión *con* descripciones se reutiliza para quien las pide *sin* ellas —contiene todo lo que tendría la otra y algo más—, pero pedirlas cuando lo guardado no las tiene genera una conversión nueva, con su propio `file_id`.

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
| En espera | `queued`, `waiting_for_memory`, `waiting_for_dependencies` (§5.6) |
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

**Cualquier otra clave de `progress` es informativa** —existe, puede ser útil para pintar, y puede cambiar o desaparecer sin subir versión de contrato—. Los contadores son un **agregado**: te dicen cuántas invocaciones van, no cuáles. El detalle por invocación tiene endpoint propio y tipado desde 2.9: §8.1.

`execution_summary` sí es contrato, y es la vía estable para saber quién respondió:

| Campo | Contenido |
|---|---|
| `requested_model` | El `target_model` que pediste, o `null` |
| `served_by` | El que respondió de verdad |
| `models_used` | Todos los que intervinieron |
| `fallback_used` | `true` si respondió uno distinto del pedido |

Es `null` mientras la tarea no ha elegido modelo y en el carril de ingesta. Los mismos datos siguen en `result` por compatibilidad, pero `result` no tiene contrato: no construyas lógica sobre él.

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

**Los enlaces del agente vienen cotejados.** Si la respuesta de un `agent` cita URLs, el broker las compara con las que el agente llegó a mirar de verdad —las que pidió abrir con `fetch_url` y las que aparecieron en los resultados de sus skills— y lo deja en `result.agent.citations`:

| Campo | Contenido |
|---|---|
| `citations.cited` | Cuántos enlaces distintos cita la respuesta |
| `citations.unsupported` | Los que no salen de ninguna consulta del agente |

La comprobación es determinista (comparación de URLs normalizadas: sin `www.`, sin barra final, sin fragmento) y **no censura nada**: la respuesta se entrega íntegra y esto la acompaña. Un `unsupported` no vacío no significa que la respuesta sea falsa, significa que esos enlaces no los vio el agente en esta tarea — típicamente los recuerda de su entrenamiento, y ahí es donde suele haber URLs que ya no existen. El campo no aparece cuando la respuesta no cita ningún enlace, que es el caso normal.

**Un consenso puede llegar degradado.** Si el árbitro falla, el broker prueba otro y, si tampoco sintetiza, entrega la mejor propuesta en lugar de fallar: prefiere una respuesta sin contrastar a ninguna respuesta. Se ve en dos campos:

| Campo | Contenido |
|---|---|
| `consensus.synthesized` | `false` si respondió una propuesta porque ningún árbitro sintetizó |
| `arbiter_failures` | Los árbitros descartados, con `model`, `code` y `message` |

Con `synthesized: false`, `model_used` es el proposer que respondió y `consensus.warnings` lo dice en texto. Si tu interfaz presenta la respuesta como «consenso de N modelos», mira este campo antes: en ese caso contestó uno solo.

### 8.1 Telemetría por invocación

`GET /api/v1/tasks/{task_id}/invocations` — misma sesión y misma autorización que el resto de `/api/v1`. Devuelve `{task_id, items[]}`, una entrada por llamada a un modelo, en orden cronológico. Un `task_id` inexistente devuelve el mismo `404 TASK_NOT_FOUND` que `GET /api/v1/tasks/{id}`.

| Campo | Contenido |
|---|---|
| `invocation_id`, `role` | Identidad de la llamada. En `mixture_of_agents` hay una por proponente más la del árbitro (`role: "arbiter"`) |
| `model` | `{provider, deployment, model, role}` que atendió esa llamada |
| `status`, `error_code` | `completed`, `failed`, `started`… y el código del fallo si lo hubo |
| `tokens_input`, `tokens_output`, `cost_usd`, `latency_ms` | Lo que costó esa llamada, no el agregado de la tarea |
| `started_at`, `completed_at`, `created_at`, `updated_at` | Marcas de tiempo |
| `generation` | Parámetros **efectivos**: ver §5.7. Ojo: `max_output_tokens` puede ser menor que el pedido si hubo que recortarlo para caber en la ventana del modelo |
| `execution_fingerprint` | Con qué configuración se sirvió. Ver §8.2 |
| `excluded_from_model_learning` | Si esta invocación alimentó o no las métricas del router (§5.8) |

No se recoge nada nuevo: es lo que el broker ya guardaba, ahora bajo contrato de cliente en vez de solo bajo el panel de administración. Anunciado en `capabilities.invocation_telemetry`.

### 8.2 Huella de ejecución

Un nombre de modelo **no es una identidad**. `qwen3:8b` de hoy y `qwen3:8b` de dentro de dos meses pueden ser binarios distintos. Y los pesos tampoco bastan: la misma red, con los mismos pesos, responde distinto si cambia la plantilla de chat, el tokenizador o el runtime. Actualizar Ollama puede reescribir la plantilla sin tocar un byte del modelo, y el `digest` seguirá siendo idéntico.

Por eso cada invocación y cada entrada del catálogo (`/api/v1/models/availability` y `/api/v1/models/context`) llevan un bloque:

```json
{
  "hash": "…",
  "components": {
    "digest":        {"value": "sha256:…", "status": "verificado"},
    "chat_template": {"value": "8f2c…",    "status": "verificado"},
    "system_fingerprint": {"value": null,  "status": "no_disponible"}
  }
}
```

- `hash` es **canónico y estable**: dos arranques del broker con la misma configuración dan el mismo valor. Si vas a comparar dos respuestas separadas en el tiempo, compara este campo.
- Cada componente declara su procedencia: `verificado` (obtenido del entorno de ejecución), `declarado` (dicho por la configuración, sin comprobar) o `no_disponible`.
- **`no_disponible` no es un hueco pendiente de rellenar.** En un modelo cloud la plantilla y el tokenizador no son observables, y eso es información real sobre el límite de lo que se puede afirmar de él. Ningún componente se deduce del nombre del modelo ni de ninguna heurística.
- La huella persistida es la **vigente en el momento de servir**, no la del catálogo actual: si el proveedor cambia a mitad de una tirada larga, se ve.

El conjunto de componentes es fijo: `provider`, `deployment`, `model` (capa declarada); `digest`, `quantization`, `parameter_size`, `family`, `tokenizer`, `chat_template`, `runtime`, `runtime_version` (capa técnica); `system_fingerprint`, `remote_model`, `api_version` (lo que dice de sí mismo un proveedor remoto). Anunciado en `capabilities.execution_fingerprint`.

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
| `PROMPT_ECHOED` | El modelo devolvió su prompt en vez de responder | no — el broker lo aparta tras dos veces |
| `DEGENERATE_OUTPUT` | El modelo se quedó repitiendo una frase hasta el final | no — igual que el anterior |
| `TASK_TIMEOUT` | Se agotó `execution.timeout_seconds` | según causa |
| `ATTACHMENT_EXPANSION_FAILED` | No se pudo inyectar el documento adjunto | no |
| `TASK_RETRY_LIMIT_EXCEEDED` | La tarea se reintentó tras varios reinicios del broker | no |

Guíate por el campo `retryable` del error, no por la tabla: es el broker quien sabe si aquel fallo concreto fue transitorio.

---

## 12. Qué ha cambiado

### 2.9 — evaluación reproducible

Todo lo de esta versión es **aditivo y opcional**: si tu cliente ya funcionaba, sigue funcionando sin tocar nada.

- **`generation.seed` y `generation.top_p`** (§5.7): generación reproducible. Sin ellos el cuerpo que llega al modelo es idéntico al de antes.
- **`exclude_from_model_learning`** (§5.8): tráfico que no debe enseñarle al broker. Si mides modelos, **querrás esto**: sin él, medir un modelo le baja la fiabilidad al router y puede llegar a apartarlo del catálogo.
- **`GET /api/v1/tasks/{id}/invocations`** (§8.1): qué modelo respondió, con qué parámetros efectivos y a qué coste, invocación a invocación. Antes solo estaba bajo el panel de administración.
- **`execution_summary`** en el estado de tarea (§8): `served_by` y `fallback_used` tipados, sin parsear `result`.
- **Huella de ejecución** (§8.2) en el catálogo y en cada invocación: la configuración exacta que produjo una respuesta.
- **`GET /api/v1/groups/{group}`** (§5.9): estado agregado de una tanda. Si mandas las tareas de una en una esperando cada respuesta, esto es lo que te permite mandarlas todas de golpe y sondear una sola cosa.

### 2.8 — dependencias, guardarraíles del agente y la frontera de las herramientas

**Lo nuevo que puede que quieras usar:**

- **Dependencias entre tareas** (§5.6): `group`, `depends_on` y `depends_on_group`, con el estado nuevo `waiting_for_dependencies`. Trátalo como cualquier otro estado no terminal: sigue sondeando y no hagas nada. Anunciado en `capabilities.task_dependencies`.
- **`result.warnings`**: lista de avisos en texto sobre cómo se ejecutó la tarea. Hoy la usan las dependencias incumplidas. Si tu interfaz muestra la respuesta sin mirar este campo, no se enterará de que se construyó sobre trabajo incompleto.
- **`result.agent.citations`**: los enlaces que cita un agente, cotejados contra los que consultó de verdad. `unsupported` son los que no salen de ninguna consulta suya.
- **Segunda ronda de consenso**: `max_rounds: 2` ya hace algo (§6.2). Antes se aceptaba y se ignoraba en silencio, y `consensus.rounds` devolvía siempre `1`.
- **Herramientas MCP** (§6.3): `agent.mcp_servers` ofrece al modelo las herramientas de servidores MCP que el operador haya configurado en el broker. Lo disponible está en `capabilities.mcp_servers`; si viene vacío, no hay nada configurado y no tienes que hacer nada.

**Un cambio de comportamiento que debes revisar:**

**La frontera de datos ahora cubre las herramientas.** Con `risk.data_classification` en `confidential` o `local_only`, pedir explícitamente `web_search` o `fetch_url` —en `agent.skills` o en `proposer_skills`— **falla con `422`**. Antes se aceptaba: el modelo se quedaba en local y la consulta salía igual a un buscador, que hacía la promesa de la clasificación falsa a medias. Si no eliges skills, la lista por defecto se acota sola a las locales y no falla nada. La lista está en `capabilities.agent_skills_egress`.

**Detalles del bucle agéntico que puede que notes:**

- Al agotar `max_iterations`, la tarea termina `completed` con una última respuesta sin herramientas en vez de con un mensaje de error (§6.3). `result.agent.final_turn` lo indica.
- Nuevo `stop_reason: "context_exhausted"` cuando la conversación desborda la ventana del modelo.
- Al agotar `max_cost_usd`, se entrega lo último que dijo el modelo en vez de solo el aviso.

### Desde 2.5

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
