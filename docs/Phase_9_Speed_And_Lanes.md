# Fase 9 — Velocidad de respuesta y carriles de trabajo

*26 de julio de 2026 · contrato 2.6*

## Para empezar: qué problema resuelve

Dos quejas concretas del uso diario.

La primera: **el broker tardaba más de lo necesario**. Tendía a elegir modelos locales, que son los lentos, incluso cuando había modelos en la nube libres y rápidos. El criterio del usuario es sencillo: cuanto menos espera quien pregunta, mejor.

La segunda: **la mitad del trabajo era invisible**. Convertir un PDF a texto puede llevar minutos y consume la máquina entera, pero esas conversiones no aparecían ni en la cola, ni en las tareas activas, ni en el historial. El panel podía decir «sin tarea activa» con dos conversiones dentro comiéndose la CPU.

Lo que sigue explica cómo se resolvieron, empezando por lo que se ve y terminando en el detalle.

---

## 1. Por qué el broker prefería lo lento

No era una preferencia: era que **los modelos cloud ni siquiera llegaban a competir**. Había cuatro cerrojos, y una app tenía que acertar tres a la vez para abrirlos:

| Cerrojo | Valor por defecto |
|---|---|
| `risk.data_classification` | `internal` |
| `model_requirements.cloud_allowed` | `false` |
| `model_requirements.allowed_providers` | `["ollama"]` |
| Filtro de catálogo por deployment | consecuencia de los dos anteriores |

Una app que declarase honestamente su clasificación de datos y no dijera nada más se quedaba en local sin enterarse.

**La solución:** `risk.data_classification` es el mando único de privacidad, y de él se derivan los otros dos cuando el cliente no se pronuncia. `public` e `internal` abren el cloud; `confidential` y `local_only` lo cierran. `confidential` pasó a comportarse como `local_only` — antes solo marcaba `high_stakes` para el meta-router y la tarea podía salir a la nube igual que una `internal`, lo cual prometía en el nombre más de lo que cumplía.

La garantía no se debilita: un `cloud_allowed: false` explícito se respeta siempre, y una clasificación restrictiva **no** se puede abrir con un `cloud_allowed: true`. Lo que desaparece es la trampa de que el silencio del cliente se resolviera de tres formas distintas.

Ver `app/schemas.py`: `classification_allows_cloud()`, `LOCAL_ONLY_CLASSIFICATIONS`, `TaskCreateRequest.enforce_data_boundary`.

---

## 2. El ranking dejó de puntuar y empezó a contar segundos

El score anterior combinaba fiabilidad, latencia y coste normalizados en un número sin unidades. Tenía dos defectos:

- **El coste subvencionaba al lento.** Un modelo local registra `cost_usd = 0.0`, así que se llevaba siempre la puntuación máxima del componente coste, compensando su peor latencia.
- **Un 0,734 no se puede explicar** ni contrastar contra un cronómetro, ni enseñar en un panel.

Ahora el criterio es el tiempo esperado hasta una respuesta **correcta**, en segundos:

```
                       carga_en_frío + (tokens_petición + salida_media) / ritmo
tiempo_esperado  =  ───────────────────────────────────────────────────────────
                                        tasa_de_éxito
```

Cada término sale de una medida:

- **`ritmo`** — tokens por segundo, de dividir la latencia medida entre los tokens que la provocaron. Sin esto, un modelo medido con preguntas de dos líneas parecía igual de rápido enfrentado a un documento de 40.000 tokens.
- **`carga_en_frío`** — la diferencia entre la media en frío y la media en caliente del propio modelo. Es coste fijo (cargar un modelo de 30B cuesta lo mismo tanto si luego se le piden diez tokens como diez mil), así que se suma aparte y solo cuando el modelo no está ahora mismo en VRAM. No es una constante inventada: para medirlo, cada invocación registra en `model_invocations.was_loaded` si el modelo estaba cargado al empezar.
- **`tasa_de_éxito`** — con suavizado de Laplace. La división no es una penalización arbitraria: con probabilidad de éxito *p*, el número esperado de intentos es 1/*p*. Un modelo que falla la mitad de las veces tarda el doble en dar algo útil.

**A un modelo sin evidencia no se le inventa un tiempo.** Va detrás de todos los medidos — no compite porque no se sabe nada de él, no porque se le suponga malo. Los tres pesos `success_weight` / `latency_weight` / `cost_weight` desaparecieron con el score: la fórmula ya contiene fiabilidad y velocidad, y el coste lo acota `max_cost_usd`, que es un presupuesto duro.

Ver `app/model_timing.py` y `RoutedModelProvider._rank_candidates`.

---

## 3. Sondeo en sombra: aprender sin hacer esperar

Con 141 modelos en catálogo y `min_invocations: 3` por tipo de tarea, medirlos todos sirviendo peticiones reales serían ~1.700 invocaciones lentas. Nunca se llegaría, y por el camino el usuario pagaría cada exploración.

La salida fue separar dos cosas que eran el mismo acto: **responder** y **aprender**. Cuando una tarea ya ha terminado, se invoca a **un** aspirante con el mismo prompt solo para cronometrarlo, y su salida se descarta.

Reglas, cada una con su motivo:

- **Un aspirante cada vez**, rematando primero al que ya está a medias. Repartir sondeos entre decenas de candidatos deja a todos con dos invocaciones sueltas, que no sirven para nada.
- **Hereda la frontera de datos de la tarea que lo origina.** Los candidatos salen de `RoutedModelProvider.eligible_catalog()`, la misma cadena de filtros que usa la selección real. Una tarea confidencial solo puede sondear modelos locales; sin esto, el sondeo sería una fuga de datos con otro nombre.
- **A los locales solo con la máquina ociosa**, porque compiten por la VRAM. Los cloud no consumen recursos locales y se sondean siempre que la clasificación lo permita.
- **Solo tras una tarea completada.** Con un prompt que acaba de fallar, el aspirante fallaría también y quedaría marcado como poco fiable por un defecto ajeno.

Detalle que decide si el invento funciona: el sondeo corre por un **carril de ejecución propio** (`_background_inference_slot`), no por el semáforo que sirve respuestas. Si ocupara ese slot, la siguiente tarea de la cola esperaría por él — habríamos trasladado la espera del usuario actual al siguiente.

Configuración en `shadow_probe` (`enabled`, `probe_local_models`, `max_prompt_chars`). Ver `app/shadow_probe.py`.

---

## 4. Carriles de trabajo: la carga real de la máquina

Las conversiones vivían fuera de todo: `asyncio.create_task` sobre un `set` **sin límite**, estado propio en `ingested_files`, ninguna fila en `tasks`. Cinco subidas eran cinco conversiones simultáneas compitiendo con la inferencia, y ninguna aparecía en el panel.

La trampa a evitar: el broker tiene `max_active_workflows: 1` **validado como invariante**. Si las conversiones hubieran entrado a competir por ese slot único, subir un PDF congelaría la cola de inferencia — exactamente lo contrario de lo que persigue el punto anterior.

La solución fue unificar el **trabajo** sin unificar la **capacidad**: columna `tasks.kind` con dos carriles.

| Carril | Capacidad | Estados activos |
|---|---|---|
| `inference` | `max_active_workflows` (1, invariante) | `routing`, `generating`, `proposing`, … |
| `ingestion` | `ingestion.max_concurrent` (2, configurable) | `converting` |

`claim_next_queued_task_id` filtra `kind = 'inference'` en la consulta del invariante **y** en la de selección; `claim_next_ingestion_task` tiene su propio reclamo con límite. Dos tuplas separadas expresan la diferencia: `ACTIVE_INFERENCE_STATUSES` (lo que consume el workflow único) y `ACTIVE_TASK_STATUSES` (lo que ocupa la máquina, conversiones incluidas).

Efecto colateral valioso: `has_active_task()` cuenta ahora los dos carriles, así que el sondeo en sombra ya no puede creer que la máquina está libre teniendo dos Doclings dentro.

En el panel, tarjeta **«Carga de la máquina»** con el desglose por carril. En la API, `?kind=` filtra y su ausencia devuelve ambos.

**El `request_json` de una conversión es un descriptor, no un `TaskCreateRequest`.** Una conversión no tiene prompt, ni modelo, ni estrategia; fabricarle un contrato de inferencia sintético solo para que validara habría contaminado el contrato con un caso que no es una inferencia. Por eso `execution_strategy` y compañía son `null` en ese carril, y `get_task_request()` mira el `kind` antes de interpretar nada.

---

## 5. Cancelar una conversión ahora la para

`asyncio.to_thread` **no es interrumpible**. Cancelar la corrutina solo dejaba de esperar: el hilo de Docling seguía dentro del broker consumiendo CPU hasta terminar por su cuenta, y un OOM suyo se llevaba el proceso entero por delante.

La fase pesada se mudó a un proceso hijo (`python -m app.ingestion.worker`), que sí se puede matar. Protocolo de líneas JSON por STDOUT: `progress` (cero o más, para lo que debe verse antes de acabar, como la duración de un vídeo largo), y `result` o `error`. STDERR queda libre para el ruido de torch y Docling.

**Dos fases**, y la frontera importa:

1. **Hijo, matable:** Docling, MarkItDown, ffmpeg, Whisper, OCR. Deja las figuras del PDF escritas en `figures/` con sus marcadores en el Markdown.
2. **Padre:** describe esas figuras con el modelo de visión, usando el proveedor de siempre, y borra el directorio.

La visión se quedó en el padre porque necesita el proveedor, las credenciales y escribir en SQLite — nada de eso debe cruzar al hijo, y así la base conserva un único escritor. Las figuras cruzan como **ficheros y no como base64** en el JSON: un PDF con cien imágenes habría convertido la salida del hijo en decenas de megas.

El mando `ingestion.isolate_conversions` está en `true` en producción y en `false` en la batería de tests, que sustituye los motores por dobles dentro del propio proceso (un hijo real no vería esos monkeypatch). El camino que se despliega lo cubre `tests/test_conversion_worker.py` con subprocesos reales.

Coste operativo asumido: la primera conversión de cada fichero paga el arranque de un intérprete nuevo, y con PDF eso incluye reimportar torch. Se paga una vez por fichero, no por página.

---

## 6. La contabilidad que faltaba

Describir una figura es invocar a un modelo, y **no se registraba en ninguna parte**. El modelo de visión podía atender cientos de figuras sin acumular una línea de historial, y su consumo de GPU era invisible para el broker que decide el enrutado.

Ahora cada descripción es una invocación completa en `model_invocations`, con tokens reales (`prompt_eval_count`/`eval_count` en Ollama, `usage.*` en los OpenAI-compatibles) y latencia medida.

Se guardan bajo el tipo de tarea **`vision`**, un bucket propio. `classify_task_type` nunca devuelve ese valor, así que la selección de modelo para texto no lo consulta jamás. Meterlas en `prose` —lo más fácil— habría contaminado con la latencia de describir imágenes la medida con la que el broker elige modelo para escribir prosa.

En `/dashboard/routing` aparecen bajo el epígrafe «Visión», con su propio ritmo y tiempo estimado.

---

## Resumen de cambios en el contrato

| Cambio | Detalle |
|---|---|
| `contract_version` | `2.5` → `2.6` |
| `derived_data_boundary` | flag nuevo en capabilities |
| `work_lanes` | flag nuevo en capabilities |
| `cloud_allowed` | `bool` → `bool \| None` (deriva de la clasificación) |
| `allowed_providers` | `list[str]` → `list[str] \| None` (sin restricción) |
| `confidential` | pasa a bloquear cloud, como `local_only` |
| `TaskStateResponse.kind` | campo nuevo |
| `execution_strategy` / `_preset` / `selection_mode` | opcionales (`null` en ingesta) |
| `TaskStatus.converting` | estado nuevo |
| `POST /api/v1/dispatcher/ingestion/tick` | endpoint nuevo |
| `GET /api/v1/dashboard/tasks?kind=` | filtro nuevo |
| `GET /api/v1/dashboard/summary` → `lanes` | campo nuevo |

Guía de integración: [`../Agent_AI_Broker.md`](../Agent_AI_Broker.md), sección «Novedades del contrato 2.6».
