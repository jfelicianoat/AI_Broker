# Control del razonamiento (thinking) en el AI Broker

Estado: **análisis, sin implementar**. Fecha de las mediciones: **2026-09-12**.

Origen: una app cliente pidió poder desactivar el razonamiento de los modelos
que use, porque no lo necesita y solo le cuesta latencia y tokens. Este
documento recoge qué se puede hacer hoy, qué se midió contra cada proveedor real
y qué implicaría exponerlo en el contrato.

## 1. Qué hay hoy en el broker

El broker ya sabe apagar el razonamiento, pero **por su cuenta y solo en
Ollama**. En `app/providers/ollama.py`:

- `THINKING_MIN_OUTPUT_TOKENS = 512` y `_thinking_disabled(capabilities, max_output_tokens)`
  (línea ~78): manda `think: false` cuando el modelo declara la capacidad
  `thinking` **y** el presupuesto de salida es menor de 512 tokens. El motivo es
  evitar que el modelo agote `num_predict` razonando y devuelva `content` vacío.
- Se aplica en `generate` (~línea 856) y en `chat_tools` (~línea 914).

No hay ninguna vía para que la app lo pida: `GenerationConfig`
(`app/schemas.py:237`) solo tiene `temperature`, `max_output_tokens`, `seed` y
`top_p`.

Lo que sí existe, y es distinto, es el **rescate** del razonamiento cuando el
modelo contesta solo por ahí:

- `rescued_content()` en `app/providers/base.py` y el flag por proveedor
  `rescue_reasoning_content` (`app/config.py:731`).
- El diagnóstico `MODEL_RETURNED_ONLY_REASONING`
  (`app/providers/diagnostics.py:159`), que es el fallo típico de un modelo
  pensante con presupuesto corto.

## 2. Lo que la app puede hacer HOY, sin tocar código

1. **Elegir un modelo que no razona**, con `preferred_model` / `target_model`.
   En Ollama, sin capacidad `thinking`: `qwen3-coder:30b`, `granite4.1:30b`,
   `lfm2:24b`, `glm-ocr:latest`. Es más fiable que pedirle a un modelo pensante
   que no piense.
2. Bajar `max_output_tokens` por debajo de 512 para que salte el automático de
   Ollama. **No recomendado**: trunca la respuesta; es un efecto colateral, no
   una función.

Lo que **no** existe es filtrar la *selección* por ausencia de `thinking`:
`ModelRequirements` filtra por proveedor, coste y frontera de datos, no por
capacidad. Un `exclude_thinking` en la selección sería la alternativa al campo
de generación (ver §6).

## 3. Mediciones por proveedor (2026-09-12)

Los cinco proveedores `custom` de `broker_config.yaml` (`lmstudio`, `nvidia`,
`HCNSEC`, `lemonade`, `unsloth`) usan **todos** `adapter: openai_compatible`, así
que comparten un único punto de implementación
(`app/providers/openai_compatible.py`). Lo que divergen es qué acepta y qué
honra cada servidor.

| Proveedor | Interruptor | Veredicto |
|---|---|---|
| `ollama` (local) | `think: false` | **Verificado OK** |
| `lmstudio` (local) | `chat_template_kwargs` / `reasoning_effort` | Acepta (200) pero sin demostrar que lo honre |
| `nvidia` NIM (api) | ninguno que funcione | **Verificado negativo** |
| `HCNSEC` (cloud) | ninguno observable | El agregador nunca expone `reasoning_content` |
| `unsloth` (local) | no probable | «No model loaded» + auto-switch apagado: 400 en toda llamada |
| `lemonade` (api) | irrelevante | 0 modelos compatibles; el único de chat es R1-Distill NPU |
| `deepseek` (apagado) | no hay parámetro | Se apaga eligiendo `deepseek-chat` en vez de `deepseek-reasoner` |

### 3.1 Ollama — funciona

`qwen3.8:27b` residente, prompt «Di solo: hola»:

| petición | `content` | `thinking` | tokens de salida |
|---|---|---|---|
| por defecto | `hola` | 136 chars | 44 |
| `think: false` | `¡Hola! ¿En qué…` | 0 | 11 |

Además, mandar `think: false` a un modelo **sin** capacidad `thinking`
(`glm-ocr:latest`) devuelve **200**, no error: la versión instalada tolera el
campo. El filtro por capacidad declarada sigue siendo prudente (versiones
antiguas de Ollama respondían 400), pero no es obligatorio.

Capacidades declaradas por `/api/show`, para referencia:

- Con `thinking`: `qwen3.8:27b`, `gemma4:12b`, `nemotron-3-nano:latest`.
- Sin `thinking`: `qwen3-coder:30b`, `granite4.1:30b`, `lfm2:24b`, `glm-ocr:latest`.

### 3.2 NVIDIA NIM — verificado negativo

`openai/gpt-oss-20b`, prompt que obliga a razonar («Un tren sale a las 14:20 y
tarda 1 h 55 min. Responde SOLO la hora de llegada»):

| variante | tokens | `reasoning_content` |
|---|---|---|
| baseline | 98 | 292 chars |
| `chat_template_kwargs: {thinking: false}` | 81 | 200 chars |
| `chat_template_kwargs: {enable_thinking: false}` | 105 | 290 chars |
| system `"detailed thinking off"` | 77 | 190 chars |
| `reasoning_effort: "none"` | — | **HTTP 400** `literal_error` |

Sigue razonando en las cuatro: la variación es ruido de muestreo, no un
interruptor. Con `z-ai/glm-5.3-flash` en NIM es peor: las cuatro variantes
devuelven `content` **vacío** con ~276 chars de razonamiento, que es exactamente
`MODEL_RETURNED_ONLY_REASONING`.

**En NIM lo único real es bajar el esfuerzo (`reasoning_effort: low`), no
apagarlo.**

### 3.3 HCNSEC — no observable

- `glm-5.3-flash` rechaza `reasoning_effort: "none"` con un 400 que enumera los
  válidos: `low | medium | high | xhigh | max`. El parámetro existe, está
  validado y **no tiene «off»**.
- `thinking: {"type": "disabled"}` se acepta (200) y no cambia nada observable:
  baseline y desactivado dan resultados idénticos.
- Ese agregador **no devuelve `reasoning_content` en ningún caso**. Ahí no se
  puede ni ver el razonamiento, solo pagar la latencia.

### 3.4 LM Studio — sin demostrar

Acepta sin error (200) tanto `chat_template_kwargs: {"enable_thinking": false}`
como `reasoning_effort: "none"`, pero no se pudo demostrar que los reenvíe a la
plantilla: el único modelo pequeño disponible es
`deepseek/deepseek-r1-0528-qwen3-8b`, que razona siempre por diseño (en la
prueba quemó 78 de 80 tokens en `reasoning_content` y devolvió `content` vacío).

**Pendiente de verificar** cargando `qwen/qwen3.8-27b` en LM Studio, lo que
obliga a soltar el modelo residente de Ollama.

### 3.5 unsloth y lemonade

- `unsloth` (Unsloth Studio, `127.0.0.1:8888`): responde a `/v1/models` pero
  toda llamada a `/chat/completions` da 400 «No model loaded. Call POST
  /inference/load first. Or enable Model auto-switch (Settings > API)». No se
  puede sondear sin cargar un modelo a mano. Sirve los **mismos ficheros GGUF**
  que LM Studio (ver el comentario de `sync_include` en `app/config.py`), así
  que su comportamiento debería seguir la semántica de llama.cpp:
  `chat_template_kwargs` es ahí el mecanismo candidato.
- `lemonade`: 2 modelos, 0 compatibles. El único de chat es
  `DeepSeek-R1-Distill-Llama-8B-NPU`, que no arranca y que además no permite
  desactivar el razonamiento por diseño. Irrelevante para esto.

## 4. La lección de contrato

Los dos mecanismos se comportan de forma **opuesta**, y eso decide el diseño:

- **`reasoning_effort`**: lo valida el servidor → 400 si el literal no le
  gusta. No se puede enviar a ciegas, y `"none"` no es un valor universal.
- **`chat_template_kwargs`**: se acepta en silencio en todos y no se honra en
  ninguno de los que se pudieron medir. Es el peor caso para la telemetría: un
  `thinking_status: "sent"` que no significa nada.

De ahí tres reglas para la implementación:

1. El conmutador va en **configuración por proveedor** (al lado de
   `rescue_reasoning_content`, que ya es ese tipo de flag), con un valor por
   defecto que no manda nada.
2. `unsupported` es lo que hay que declarar salvo que se haya **verificado**
   contra ese endpoint concreto. `sent` significa «lo incluí en el cuerpo»,
   nunca «el modelo obedeció» — la misma honestidad que `seed_status`.
3. El truco del system prompt (`"detailed thinking off"` de Nemotron,
   `/no_think` de Qwen) tiene coste extra: la estrategia `single` **no tiene hoy
   punto de inyección de system prompt** (solo `agent` y `mixture`, ver
   `ROLE_SYSTEM_PROMPTS` en `app/providers/base.py:23`). Abrirlo toca el prompt
   que se cachea y la huella de ejecución.

## 5. Qué implicaría implementarlo

El molde exacto existe: es lo que se hizo con `seed`/`top_p` en el contrato 2.9.
Los mismos siete sitios:

1. **`GenerationConfig`** (`app/schemas.py:237`): `thinking: bool | None`,
   tri-estado. `None` = lo decide el broker (comportamiento actual, nadie ve un
   cambio); `False` = apágalo; `True` = no me lo apagues aunque el presupuesto
   sea corto. Hay que decidir la precedencia contra el umbral de 512, porque hoy
   el broker gana siempre.
2. **Ollama** (`app/providers/ollama.py`): `_thinking_disabled` pasa a leer
   también la petición. Cambio de tres líneas.
3. **openai_compatible** (`app/providers/openai_compatible.py`, ~línea 670 en
   `generate` y ~574 en `chat_tools`): campo nuevo en el cuerpo **solo** si la
   configuración del proveedor declara un mecanismo verificado.
4. **Telemetría**: `thinking_status` en `effective_generation`
   (`app/providers/base.py:409`) y en `EffectiveGeneration`
   (`app/schemas.py:857`), con los mismos valores que `seed_status`:
   `not_requested` / `sent` / `unsupported`.
5. **Capacidad publicada** en `BrokerCapabilitiesResponse`
   (`app/schemas.py:1119`) y subir `contract_version` en `app/main.py:838`.
6. **Propagación**: las sub-invocaciones (juez de confianza, map-reduce,
   mixture) se derivan del request original con `model_copy`, así que heredarían
   el flag solas. Decisión a tomar a conciencia: si el **árbitro** también deja
   de pensar. La app pidió que no piense el modelo que responde, no
   necesariamente el que juzga.
7. **Superficie y pruebas**: formulario del probador
   (`app/dashboard_forms.py`), `docs/Client_API.md` §5.7 y tests.

## 6. Alternativa: filtrar la selección

En vez de (o además de) un campo de generación, `ModelRequirements` podría
admitir algo tipo `exclude_thinking: true`, que descarta del catálogo elegible
todo modelo con capacidad `thinking` declarada. Ventajas: funciona en cualquier
proveedor sin depender de que el servidor honre un parámetro, y es verificable
(la capacidad está en el catálogo). Inconveniente: reduce el conjunto de
modelos, y en proveedores cloud la capacidad no siempre está declarada.

## 7. Las dos trampas

- **El ranking mide tiempo esperado.** Un modelo con razonamiento apagado es
  otro régimen de latencia. Si `model_stats` no separa los dos casos, el
  aprendizaje mezcla medias y contamina el orden para las apps que **sí**
  quieren razonamiento. Hay que resolverlo al diseñar, no después.
- **Hay modelos donde no se puede.** R1 y gpt-oss razonan por plantilla.
  Tratarlo como `seed`: declarar `unsupported` en la telemetría, no rechazar la
  tarea con 422.

## 8. Recomendación

Para la app que lo pidió: **el camino fiable es elegir modelo, no pedir que no
piense.** Si además necesita el interruptor, hacerlo por fases:

- **Fase 1** — solo Ollama: campo tri-estado + `thinking_status` + capacidad
  publicada. Es el único proveedor con un «off» verificado.
- **Fase 2** — LM Studio y unsloth, después de verificar el mecanismo con un
  modelo qwen3 cargado a mano.
- **Nunca** para nvidia y HCNSEC mientras la medición diga lo que dice: ahí el
  broker no puede prometer nada, y lo honesto es que la app restrinja
  `allowed_providers` a Ollama cuando lo que necesita es no razonar.

## Apéndice — hallazgo colateral, ajeno al thinking

Al sondear los proveedores cloud salió esto, y afecta al enrutado hoy:

- **El catálogo de `nvidia` está caducado.** `nvidia/nvidia-nemotron-nano-9b-v2`
  y `nvidia/llama-3.3-nemotron-super-49b-v1.5` están marcados `compatible` en
  `broker_config.yaml` (sondeados en julio de 2026) y hoy devuelven **410 Gone**
  (EOL 2026-08-26). `nvidia/nemotron-nano-3-30b-a3b` aparece en `/v1/models`
  pero da **404 «Not found for account»**: el listado del endpoint no coincide
  con lo que la cuenta puede llamar. Hay 63 modelos nvidia marcados compatibles;
  se desconoce cuántos están muertos.
- **`HCNSEC`, lo mismo**: `Qwen3.8-27B` (marcado `compatible`) da 404
  `openai_error` y `glm-5.2` da 503 «No available channel».

La cuarentena los irá apartando tras dos fallos definitivos, pero cada uno se
come un intento de una tarea real. Revalidar ambos catálogos con el sondeo
existente (`probe_skip_compatible: false`) antes de tocar cualquier otra cosa.
