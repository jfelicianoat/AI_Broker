# Referencia de `broker_config.yaml`

*Actualizada el 23 de agosto de 2026 · contrato 2.9*

Todo lo configurable del broker, sección por sección, con su valor por defecto y
—lo que suele faltar— **por qué existe cada ajuste y qué se rompe al moverlo**.
La fuente de verdad es `app/config.py`: si algo diverge, manda el código y este
documento tiene un error que hay que corregir.

## Cómo se lee y se escribe

- **El fichero es opcional y parcial.** `load_config` parte de los valores por
  defecto y funde encima lo que traiga el YAML, sección a sección
  (`_deep_merge`). Borrar una clave la devuelve a su valor por defecto; borrar
  el fichero entero arranca un broker con toda la configuración por defecto.
- **Las claves desconocidas se ignoran**, no rompen el arranque. Es lo que
  permite que un YAML antiguo siga cargando cuando un ajuste desaparece (los
  pesos `success_weight` / `latency_weight` / `cost_weight` del score
  multiobjetivo, retirados en julio de 2026, son el ejemplo vivo).
- **El panel escribe este fichero.** La página *Configuración* guarda de forma
  atómica, deja el anterior en `broker_config.yaml.bak` y detecta la edición
  concurrente con una huella SHA-256 del fichero: si alguien lo tocó a mano
  mientras había un formulario abierto, el guardado se rechaza en vez de pisar
  el cambio.
- **Casi todo aplica en caliente.** Los servicios leen el `BrokerConfig`
  compartido en vivo. Las excepciones son las que dependen del proceso o de la
  construcción de la app: `server.*`, `persistence.database` y
  `logging.*`, que exigen reiniciar.
- **Ninguna credencial vive aquí.** Los campos `*_api_key_env` nombran una
  variable de entorno; las claves se guardan en el keyring del sistema. El YAML
  guarda el *nombre*, nunca el secreto.

---

## `server`

Lo que no cambia sin reiniciar el proceso.

| Clave | Defecto | Qué hace |
|---|---|---|
| `host` | `127.0.0.1` | Interfaz de escucha. Fuera de loopback se activa el arranque *fail-closed* (ver abajo) |
| `port` | `8765` | Puerto del API y del panel |
| `workers` | `1` | **Invariante validado**: cualquier otro valor impide arrancar. Con N workers habría N dispatchers y N recuperaciones de arranque sobre la misma SQLite: re-encolado de tareas en ejecución y gasto duplicado. El runner pasa la instancia de la app a Uvicorn, no un import string, precisamente para que no pueda multiplicarse |
| `cors_enabled` | `false` | Permite que una web servida desde otro origen llame al API. Exige `cors_allow_origins` |
| `cors_allow_origins` | `[]` | Orígenes exactos autorizados (esquema + host + puerto, ≤32) |
| `admin_token_env` | `AI_BROKER_ADMIN_TOKEN` | Variable de entorno de la que sale el token de administración |
| `admin_keyring_service` | `ai-broker` | Servicio del keyring donde buscar el token si no está en el entorno |
| `admin_keyring_username` | `dashboard_admin_token` | Usuario del keyring para ese token |
| `allow_unauthenticated_lan` | `false` | Único opt-out del arranque fail-closed |
| `publish_session_token` | `none` | `keyring` publica el token de cada arranque para los clientes de la misma máquina (ver abajo) |
| `session_token_keyring_username` | `session_admin_token` | Entrada del keyring donde lo deja. **No** es la que el broker lee |

**CORS.** Activarlo sin orígenes **impide arrancar**, y `*` se rechaza: el API viaja con token de administración, y abrirlo a cualquier origen lo entrega a cualquier web que visite quien lo tenga en marcha. Con orígenes declarados se monta el middleware con `allow_credentials=False` —la autenticación es la cabecera `X-Admin-Token`, que la app cliente pone a mano; permitir cookies expondría además la sesión del panel— y con los métodos y cabeceras que el broker usa. Un origen con ruta (`https://app.local/panel`) también se rechaza: el navegador compara esquema+host+puerto, así que una ruta ahí no restringe nada, solo hace que la entrada no case nunca.

**Arranque fail-closed.** Con `host` fuera de loopback y sin token admin (ni en
entorno ni en keyring), el broker **se niega a arrancar**. Un panel que expone
prompts y resultados en una LAN sin credencial no es un despliegue, es una
fuga. `allow_unauthenticated_lan: true` lo permite explícitamente y queda
registrado con un warning en cada arranque.

Con token configurado, exigen credencial las mutaciones y las lecturas que
contienen prompts o resultados, tanto en `/api/v1` como en el panel.

**Publicación del token de sesión.** El token se genera nuevo en cada arranque,
vive en la variable de entorno del proceso del broker y se imprime en su
consola. Eso basta para una persona delante y no basta para nada más: un
proceso co-ubicado que arranca con la máquina —Wake-on-LAN, tarea programada—
no ve esa consola ni hereda ese entorno, y la única alternativa que le quedaba
era recibir el token por la red, que es justo lo que no puede pasar.

Con `publish_session_token: keyring`, cada arranque deja el token de esa sesión
en el almacén de credenciales del SO (en Windows, el Administrador de
credenciales: cifrado, con ACL del usuario, fuera de logs y de líneas de
comando), bajo `admin_keyring_service` / `session_token_keyring_username`. Un
cliente local lo lee con `keyring.get_password("ai-broker",
"session_admin_token")` y, ante un `403`, vuelve a leerlo y reintenta una vez.

Dos entradas distintas, y conviene no confundirlas:

| Entrada | Quién la escribe | Quién la lee |
|---|---|---|
| `dashboard_admin_token` | El operador, para fijar un token estable | El broker, como *fallback* de la variable de entorno |
| `session_admin_token` | El broker, en cada arranque | Los clientes co-ubicados. El broker **nunca** la lee |

Pisar la primera con el token efímero convertiría un token de sesión en uno
permanente; por eso son entradas separadas.

`none` es el defecto para que un despliegue existente no empiece a escribir
credenciales en el llavero de su dueño sin que él lo haya decidido. No hay modo
"fichero" a propósito: sería el mismo secreto en claro, con permisos que
dependen de dónde caiga el directorio y sobreviviendo a un apagado sucio. Un
fallo del llavero **no impide arrancar** —el broker sirve igual y el token
sigue en consola— pero queda un warning `admin.session_token_publish_failed`:
el síntoma en el otro extremo son `403` sin explicación.

---

## `persistence`

| Clave | Defecto | Qué hace |
|---|---|---|
| `database` | `state/broker.db` | Ruta de la SQLite. Cambiarla exige reiniciar |
| `journal_mode` | `WAL` | Modo de journal. WAL es lo que permite leer mientras el dispatcher escribe |
| `events_retention_days` | `30` | Días que se conservan los eventos de tareas **terminales**. `0` = no podar nunca |
| `artifacts_retention_days` | `0` | Ídem para los artefactos en disco de tareas terminales |
| `files_retention_days` | `0` | Ídem para los ficheros ingeridos `ready`/`failed` |

Las podas corren **en el arranque**, no en segundo plano. Las conversiones en
curso no se podan jamás. Aviso operativo: una retención corta de ficheros con
colas largas es mala combinación — una tarea encolada que referencie un fichero
podado falla en el despacho. El evento `prompt.compressed` está exento de la
poda de eventos: es la única prueba de qué se le mandó de verdad al modelo.

---

## `processing`

El motor de la cola y la memoria de los modelos locales.

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `max_active_workflows` | `1` | `== 1` | **Invariante validado**. Un solo workflow de inferencia activo en todo el broker |
| `max_parallel_invocations` | `"auto"` | `"auto"` o `>= 1` | Invocaciones simultáneas dentro de un mixture `slow`. `auto` usa la fórmula conservadora compartida entre el planificador y el semáforo del router: si divergieran, el plan prometería un paralelismo que el router no concede |
| `queue_max_size` | `1000` | | Tope de la cola |
| `task_timeout_seconds` | `3000` | 1–86400 | **Techo** del plazo de una tarea: ninguna pasa de aquí, pida lo que pida. Por encima de `default_task_timeout_seconds` a propósito — por debajo lo dejaría de adorno |
| `default_task_timeout_seconds` | `2400` | 1–86400 | Plazo que se le pone a la petición que no trae `execution.timeout_seconds`. El plazo efectivo es el **menor** de este y `task_timeout_seconds`: subir uno solo no cambia nada. 2400 y no 600 porque un modelo local grande paga la carga desde disco dentro de su primera invocación |
| `max_task_attempts` | `3` | 1–100 | Intentos antes de `TASK_RETRY_LIMIT_EXCEEDED`. Cada arranque devuelve a `queued` las tareas que estaban activas, con `attempt+1` |
| `unload_after_task` | `true` | | Descargar los modelos locales al terminar cada tarea |
| `idle_unload_seconds` | `0.0` | 0–86400 | Segundos de broker parado tras los que se descargan los modelos locales. `0` = nunca |
| `auto_dispatch` | `true` | | Dispatcher autónomo. Con `false` solo avanza con `POST /api/v1/dispatcher/tick` |
| `dispatcher_interval_seconds` | `0.1` | >0, ≤60 | Cada cuánto mira el dispatcher si hay trabajo |
| `provider_mode` | `real` | `real` \| `bootstrap` | `bootstrap` sustituye a los proveedores reales por uno de arranque, para levantar el broker sin ningún runtime instalado |
| `dependency_wait_seconds` | `2.0` | 0.1–3600 | Cada cuánto reintenta una tarea en `waiting_for_dependencies`. Corto a propósito: lo que la bloquea está en esta misma cola y suele terminar en segundos |

> **El plazo de una tarea es una cadena, no un número.** Se corta por el primero que venza: `execution.timeout_seconds` de la petición (o `processing.default_task_timeout_seconds` si no lo trae), el techo `processing.task_timeout_seconds`, y el `timeout_seconds` del proveedor que atiende la invocación. Subir uno solo no sirve de nada si otro corta antes; el arranque avisa (`config.timeout_incoherent`) y el error `TASK_TIMEOUT` nombra cuál mandó (`timeout_bound_by`).

**Los dos ajustes de memoria se leen juntos.** `unload_after_task: true` deja la
máquina como la encontró después de cada tarea; el precio es que dos tareas
seguidas con el mismo modelo pagan la carga dos veces y el ranking nunca ve un
modelo caliente al que preferir. Con `false`, el modelo se queda residente
(`keep_alive: -1`) y quien devuelve la memoria es `idle_unload_seconds`. Con
`unload_after_task: true`, el plazo **no hace nada**. Detalle en
[`Phase_6_Operations.md`](Phase_6_Operations.md).

---

## `prompt_compression`

| Clave | Defecto | Qué hace |
|---|---|---|
| `enabled` | `true` | Comprimir el prompt antes de mandarlo al proveedor |
| `level` | `medium` | `light` \| `medium` \| `aggressive` |
| `min_chars` | `40` | Por debajo de este tamaño el prompt va tal cual: en un prompt corto cada palabra cuenta |

La compresión es determinista y por reglas (nunca un LLM), `content.prompt` se
persiste intacto y los embeddings no se comprimen jamás. Con adjuntos, la
compresión pasa a `off` salvo que la tarea la pida explícitamente: comprimir
tablas o código de un documento los corrompe. Detalle en
[`Prompt_Compression.md`](Prompt_Compression.md).

---

## `ingestion`

Conversión de ficheros adjuntos. Detalle completo en
[`Phase_7_File_Ingestion.md`](Phase_7_File_Ingestion.md).

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `true` | | Con `false`, `capabilities.file_ingestion` es `false` y el carril de conversiones no existe |
| `storage_dir` | `state/files` | | Dónde viven originales y Markdown |
| `max_file_mb` | `100` | 1–32768 | Tope de subida. La subida es en streaming a disco, así que el límite real es espacio, no RAM |
| `max_pdf_pages` | `500` | 1–10000 | Tope de páginas de un PDF |
| `ocr_enabled` | `true` | | Habilita el OCR: por página en PDF escaneados, y a petición en imágenes (`ocr=true`) |
| `ocr_languages` | `[es, en]` | | Idiomas del OCR |
| `conversion_timeout_seconds` | `900` | 10–43200 | Plazo por conversión. Al agotarse, la tarea de ingesta queda `failed` aunque el hilo siga drenando |
| `max_concurrent` | `2` | 1–16 | Conversiones simultáneas del carril de ingesta. Una sola desaprovecha la máquina con ficheros pequeños; tres Doclings con OCR compiten de verdad con la inferencia |
| `isolate_conversions` | `true` | | Ejecutar la fase pesada en un **proceso hijo**. Es lo que hace que cancelar una conversión la pare de verdad (un hilo de Python no se puede matar, un proceso sí) y que un OOM de torch mate al hijo y no al broker |

### `ingestion.images` — figuras **embebidas** en un documento

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `false` | | Valor por defecto de la política; cada subida puede fijar el suyo con `describe_images` |
| `base_url` | `http://127.0.0.1:11434/v1` | | Endpoint OpenAI-compatible del modelo de visión |
| `model` | `""` | | Modelo que describe las figuras |
| `api_key_env` | `null` | | Variable de entorno con la clave, si el endpoint la pide |
| `timeout_seconds` | `120.0` | >0, ≤600 | Plazo por figura |
| `max_images` | `20` | 0–200 | Tope de figuras descritas por documento; el resto queda marcado como omitida |

**No confundir con las imágenes adjuntas.** Esta sección gobierna las figuras
*dentro* de un PDF o un DOCX, donde el resto del documento es texto y la figura
tiene que ocupar su sitio en él. Una imagen adjuntada suelta no se describe
nunca: viaja entera hasta un modelo con visión.

### `ingestion.transcription` — audio y vídeo

| Clave | Defecto | Qué hace |
|---|---|---|
| `enabled` | `true` | Transcripción local con faster-whisper |
| `model_size` | `small` | `tiny`/`small`/`medium`/`large-v3`… Se descarga y cachea en el primer uso |
| `device` | `auto` | `auto` \| `cpu` \| `cuda` |
| `language` | `null` | `null` = autodetección |
| `ffmpeg_path` | `ffmpeg` | Binario para extraer el audio de un vídeo |

---

## `sandbox`

Ejecución de código generado por modelos (skill `run_code`). Detalle en
[`Phase_8_Sandbox.md`](Phase_8_Sandbox.md).

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `false` | | Doble opt-in: sin esto, pedir `run_code` da `409 SANDBOX_DISABLED` |
| `docker_path` | `docker` | | Binario de Docker |
| `image` | `python:3.12-slim` | | Imagen base. La del repo (`ai-broker-sandbox:latest`) trae pandas, numpy, matplotlib y openpyxl |
| `timeout_seconds` | `60.0` | 5–600 | Plazo por ejecución |
| `memory_mb` | `1024` | 64–16384 | Memoria del contenedor |
| `cpus` | `2.0` | >0, ≤32 | CPUs del contenedor |
| `pids_limit` | `256` | 16–4096 | Tope de procesos |
| `max_output_chars` | `8000` | 500–100000 | Recorte de stdout/stderr devuelto al modelo |
| `work_volume_mb` | `256` | 32–4096 | tmpfs `/work` cuando hay adjuntos que copiar. **Validado**: debe ser menor que `memory_mb`, porque el tmpfs cuenta contra la memoria del contenedor |

**Lo que no es configurable, a propósito:** sin red, sin privilegios, rootfs de
solo lectura y ningún bind mount del host. Esas fronteras no tienen ajuste
porque no son una preferencia.

---

## `mcp`

Servidores MCP (transporte stdio) como herramientas del agente. Detalle en
[`MCP_Servers.md`](MCP_Servers.md).

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `false` | | Interruptor general |
| `servers` | `[]` | ≤16 | Lista de servidores; los `id` deben ser únicos (validado) |
| `startup_timeout_seconds` | `20.0` | 1–300 | Plazo del handshake inicial (`initialize` + `tools/list`), que se paga una vez por proceso |

Cada entrada de `servers`:

| Clave | Defecto | Qué hace |
|---|---|---|
| `id` | *(obligatorio)* | `^[a-z0-9][a-z0-9_-]*$`. Prefija las tools como `mcp__<id>__<tool>`, para que un servidor no pueda suplantar una skill del broker |
| `enabled` | `true` | |
| `command`, `args` | *(obligatorio / `[]`)* | Proceso a lanzar; `args` ≤ 32 |
| `env` | `{}` | Variables extra del hijo. Un valor `env:NOMBRE` toma el valor de esa variable del entorno del broker, para no escribir credenciales en el YAML |
| `data_boundary` | *(obligatorio)* | `local` \| `egress`. **Sin valor por defecto a propósito**: deducirla del transporte marcaría como local un servidor stdio que por dentro llama a internet, y `local_only` volvería a prometer lo que no cumple |
| `timeout_seconds` | `30.0` | Tope por llamada: un servidor colgado no puede quedarse con el turno |

Sin transporte HTTP, también a propósito: el broker no depende de ninguna nube
para funcionar, y un servidor remoto haría de cada herramienta una salida de
datos más difícil de ver que un proveedor cloud.

---

## `resources`

Memoria de la máquina y admisión de trabajo local.

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `local_vram_budget_gb` | `64.0` | | VRAM que el broker considera suya |
| `vram_safety_margin_gb` | `6.0` | | Lo que se resta del presupuesto antes de admitir nada |
| `unified_memory_budget_gb` | `null` | 1–4096 | **Solo en máquinas de memoria unificada** (APU tipo Strix Halo, Apple Silicon): pool completo utilizable. Declarado, sustituye a `local_vram_budget_gb` como techo de admisión, de modo que un modelo mayor que la VRAM se reparte con la RAM compartida en vez de rechazarse. Vacío = GPU discreta, donde salirse de la VRAM sí es caer por el barranco del PCIe. **Validado**: no puede ser menor que `local_vram_budget_gb` |
| `gpu_layer_offload` | `true` | | Repartir capas entre GPU y CPU cuando un modelo no cabe entero, calculando cuántas caben y **diciéndoselo** a Ollama (`options.num_gpu`) en vez de dejar que lo adivine. El precio es velocidad; con `false`, un modelo mayor que el presupuesto es terminal (`VRAM_MODEL_TOO_LARGE`) |
| `gpu_offload_reserve_ratio` | `0.2` | 0–0.9 | Parte del presupuesto que no se dedica a los pesos: caché KV, buffers y contexto. Reservar de menos es lo que hace fallar la carga justo al final, tras leer decenas de GB de disco |
| `gpu_context_sizing` | `true` | | Pedir el contexto que la tarea necesita en vez de dejar que el runtime reserve el del modelo entero. Solo aplica a la generación de un turno: en el bucle agéntico la conversación crece y reajustar en cada llamada obligaría a recargar el modelo |
| `gpu_context_minimum_tokens` | `4096` | 512–1e6 | Suelo de lo anterior: los tokenizadores reales no coinciden con la estimación del broker, y quedarse corto trunca la respuesta |
| `max_loaded_local_models` | `"auto"` | `"auto"` o ≥1 | Techo por **conteo** de modelos locales cargados a la vez, además del de memoria. `auto` = la capacidad paralela de inferencia. Una tarea que llega con el cupo lleno no falla: cede el turno como ante cualquier falta de memoria |
| `scheduling_policy` | `adaptive` | `adaptive` \| `parallel` \| `waves` \| `sequential` | Política de planificación por defecto de un mixture `slow` cuyo cliente no la declare. Un `execution.scheduling` explícito manda siempre |
| `allow_execution_waves` | `true` | | Permitir el plan por oleadas en un mixture `slow` |
| `memory_wait_seconds` | `20.0` | 1–3600 | Cada cuánto reintenta una tarea en `waiting_for_memory` |
| `memory_reserve_after` | `5` | 1–100 | Turnos cedidos tras los que la tarea reserva el suyo. Es el freno a la inanición: sin él, una petición de 48 GB nunca correría mientras sigan llegando peticiones de 8 GB |
| `memory_reserve_window_seconds` | `300.0` | 10–86400 | Cuánto dura esa reserva. Al expirar, la cola vuelve a fluir: si la memoria no se libera jamás, la reserva no puede convertirse en un bloqueo permanente |

**El cupo de modelos no es lo mismo que el de memoria.** La memoria sola no basta en una máquina holgada: cuatro modelos medianos caben y se pelean por el mismo bus y los mismos núcleos, y ejecutarlos a la vez va peor que en serie. Antes de hacer esperar a nadie se descargan los modelos cargados que no estén sirviendo ninguna tarea; solo si los que ocupan el cupo tienen lease se cede el turno, con `LOCAL_MODEL_SLOTS_BUSY` y el motivo `model_slots` en el bloque que ve el panel. Un modelo que **ya** está cargado nunca paga el cupo: no hay carga que hacer.

La espera por memoria **no caduca**, por decisión de producto: no se descarta
trabajo por un pico de memoria. La tarea queda visible en el panel con quién le
ocupa la memoria y se cancela a mano si estorba.

---

## `routing`

Selección adaptativa: reordena los candidatos que **ya** pasaron los filtros de
elegibilidad. No cambia quién es elegible, solo a quién se prefiere.

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `adaptive_selection` | `true` | | Con `false`, el orden es el del catálogo |
| `stats_window_days` | `7` | 1–365 | Ventana de histórico: las invocaciones más antiguas no pesan |
| `min_invocations` | `3` | ≥1 | Por debajo de esto el modelo puntúa neutro. Un modelo recién añadido no queda castigado por no tener historia |
| `exploration_rate` | `0.0` | 0–1 | Probabilidad de ascender a otro candidato a la primera posición. Sin esto, el que gana en cuanto supera `min_invocations` se autorrefuerza: solo él acumula invocaciones, así que ningún otro alcanza evidencia para competir. La exploración prioriza a los que aún no llegan a `min_invocations` en ese tipo de tarea |

El ranking estima **tiempo esperado hasta una respuesta correcta**, en segundos,
no una puntuación sin unidades — de ahí que los pesos `success_weight`,
`latency_weight` y `cost_weight` ya no existan. La fórmula y sus términos están
en [`Phase_9_Speed_And_Lanes.md`](Phase_9_Speed_And_Lanes.md) §2.

---

## `model_quarantine`

Aparta modelos que han dejado de producir salida usable. Es una capa distinta
del ranking: la selección adaptativa ordena por tiempo, no por acierto, y un
modelo que falla **rápido y siempre** puede seguir puntuando bien.

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `true` | | |
| `consecutive_failures` | `2` | 1–20 | Fallos definitivos seguidos, sin ningún acierto entre medias, para apartarlo. Con uno solo, un tropiezo puntual apartaría un modelo sano |
| `window_hours` | `24.0` | >0, ≤8760 | La cuarentena caduca sola: pasada la ventana sin invocaciones, el modelo vuelve. Si sigue roto vuelve a caer tras dos intentos; si lo han reinstalado, nadie tiene que acordarse de rehabilitarlo |
| `definitive_codes` | ver abajo | | Códigos que significan "este modelo no sirve", no "ahora no se puede" |

Por defecto: `INVALID_PROVIDER_RESPONSE`, `PROMPT_ECHOED`, `DEGENERATE_OUTPUT`,
`MODEL_COMPATIBILITY_MISMATCH`, `MODEL_UNAVAILABLE`,
`MODEL_DEPLOYMENT_MISMATCH`. Quedarse sin memoria o un proveedor caído son
circunstancias del entorno y **no** cuentan. Los tres primeros son salida que el
broker ha inspeccionado y declarado inservible: quitarlos de la lista deja el
defecto sin consecuencia, y hay un test que lo vigila.

---

## `shadow_probe`

Medir modelos sin evidencia sin hacer esperar a nadie.

| Clave | Defecto | Qué hace |
|---|---|---|
| `enabled` | `false` | Off por defecto para que un despliegue nuevo no empiece a invocar modelos por su cuenta sin que su dueño lo decida. El YAML que se distribuye lo activa |
| `probe_local_models` | `true` | Los locales compiten por la VRAM con las respuestas de verdad, así que solo se les sondea con la máquina ociosa; los cloud, siempre que la clasificación de datos lo permita |
| `max_prompt_chars` | `8000` | Por encima de este tamaño no se sondea: un prompt enorme convierte cada medida en un trabajo caro |

---

## `task_affinity`

Qué modelos son *idóneos* para cada tipo de tarea. Los filtros de elegibilidad
responden a "¿puede?"; esta capa responde a "¿debería?".

| Clave | Defecto | Qué hace |
|---|---|---|
| `enabled` | `true` | |
| `exclude_always` | `*guard*`, `*safety*`, `*topic-control*`, `*-pii*`, `*-ocr*`, `ocr-*`, `*translate*`, `*detector*`, `*calibration*` | Modelos de propósito único: no son de chat y no deben entrar en ninguna rotación general |
| `exclude_by_task_type` | especialistas de código en `prose` y `long_context`; `code: []` | Exclusiones por tipo de tarea. **Validado**: solo se admiten los tipos `code`, `long_context` y `prose` |

Los patrones son fnmatch, insensibles a mayúsculas, y se comparan contra el
nombre del modelo **y** contra su `catalog_id` de models.dev: el mismo modelo se
llama distinto según el proveedor. Van anclados a separadores a propósito — un
`*ocr*` suelto casaría con "mediocre", y una exclusión de más hace tanto daño
como una de menos.

El filtro es **best-effort**: si dejara la selección sin candidatos, se ignora.
Una preferencia nunca debe convertir en irresoluble una tarea atendible, y una
preferencia explícita del cliente manda sobre ella.

---

## `strategy_router`

Meta-router para las tareas con `strategy: auto`. Tres piezas activables por
separado.

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `false` | | Con `false`, `auto` no aparece en `capabilities.strategies` y una tarea que lo pida se resuelve a `single` |
| `heuristic_classifier` | `true` | | Pieza 1: reglas deterministas, baratas y trazables |
| `confidence_escalation` | `false` | | Pieza 2: se responde con `single`, un juez puntúa 0–1 y por debajo del umbral se escala a mixture con el presupuesto restante |
| `adaptive_learning` | `false` | | Pieza 3: corrige a la heurística con la evidencia de casos previos |
| `record_cases` | `true` | | Guarda los casos en `routing_cases` desde el principio, para que la pieza 3 no arranque de cero cuando se active |
| `mixture_min_prompt_chars` | `600` | ≥0 | Umbral de longitud del prompt para considerar mixture |
| `mixture_min_budget_usd` | `0.0` | ≥0 | Por debajo de este presupuesto no se escala a mixture (es caro) |
| `escalation_min_confidence` | `0.6` | 0–1 | Umbral del juez de la pieza 2 |
| `learning_min_cases` | `5` | 1–10000 | Mínimo de casos por bucket para que el aprendizaje pueda decidir |
| `learning_escalation_threshold` | `0.5` | 0–1 | A partir de aquí el aprendizaje escala |
| `learning_failure_threshold` | `0.4` | 0–1 | A partir de aquí considera fracasada la estrategia elegida |

La clasificación es **técnica**, no de dominio: necesita datos actuales /
cálculo / URL → `agent`; deliberativa y con presupuesto → `mixture_of_agents`;
directa → `single`. Cada decisión queda como evento `strategy.routed` con
señales y motivos.

---

## `model_enrichment`

| Clave | Defecto | Rango | Qué hace |
|---|---|---|---|
| `enabled` | `false` | | Opt-in: un despliegue sin salida a internet no descarga nada |
| `url` | `https://models.dev/api.json` | | Catálogo externo, gratuito y sin clave |
| `refresh_hours` | `24.0` | >0, ≤720 | Cada cuánto se refresca la copia cacheada en disco |
| `timeout_seconds` | `20.0` | >0, ≤300 | Plazo de la descarga |

Aporta contexto real, precios por millón, corte de conocimiento y capacidades
declaradas (incluida `image_output`, la única evidencia de que un modelo *genera*
imágenes). **Nunca pisa un dato verificado** por sondeo o por el runtime: rellena
huecos.

---

## `health`

| Clave | Defecto | Qué hace |
|---|---|---|
| `sqlite_interval_seconds` | `10` | TTL de la caché del check de SQLite |
| `local_dependencies_interval_seconds` | `30` | Ídem para las dependencias locales |
| `external_providers_interval_seconds` | `300` | Ídem para proveedores externos: cada sonda es una llamada de red a un tercero |
| `disk_free_alert_gb` | `10` | Por debajo de esto, el volumen de la BD se reporta `degraded` |
| `probe_timeout_seconds` | `5.0` | Deadline de cada sonda; al agotarse se reporta `unavailable` sin bloquear la respuesta |

`/health` y `/health/ready` reutilizan el último resultado dentro del intervalo
en vez de sondear en cada GET. SQLite es lo único que determina *readiness*: un
proveedor caído no impide aceptar trabajo en cola.

---

## `logging`

Requiere reiniciar para aplicar.

| Clave | Defecto | Qué hace |
|---|---|---|
| `level` | `INFO` | |
| `directory` / `filename` | `logs` / `ai-broker.log` | |
| `max_bytes` | `10485760` | Rotación por tamaño |
| `backup_count` | `5` | Ficheros rotados que se conservan (1–100) |
| `console_enabled` | `true` | |

Formato JSON Lines. El access log registra método, ruta, código, duración y
cliente — **nunca** cuerpos, prompts, respuestas, cabeceras de autorización ni
claves.

---

## `providers`

### `providers.ollama`

| Clave | Defecto | Qué hace |
|---|---|---|
| `enabled` | `true` | |
| `base_url` | `http://127.0.0.1:11434` | |
| `timeout_seconds` | `2400` | Plazo de inferencia. Alineado con `processing.default_task_timeout_seconds` y no en 300 como los proveedores remotos: Ollama es siempre local y ahí la espera larga es un modelo cargando desde disco, no un fallo |
| `unload_timeout_seconds` | `10` | Plazo de la descarga de un modelo |
| `catalog_cache_seconds` | `5.0` | TTL del catálogo (`/api/tags` + `/api/show`) |

### `providers.deepseek`

| Clave | Defecto | Qué hace |
|---|---|---|
| `enabled` | `false` | Deshabilitado por defecto |
| `base_url` | `https://api.deepseek.com` | |
| `timeout_seconds` | `300` | |
| `api_key_env` | `DEEPSEEK_API_KEY` | Primero el entorno, después el keyring; sin credencial, `CREDENTIALS_UNAVAILABLE` y nunca un fallback silencioso |
| `keyring_service` / `keyring_username` | `ai-broker` / `deepseek_api_key` | |
| `default_model` | `deepseek-chat` | |
| `context_window` | `64000` | |
| `input_cost_per_million` / `output_cost_per_million` | `0.0` | **Revísalos antes de habilitar el proveedor**: las tarifas son configuración operativa y con ellas a cero el control de presupuesto no controla nada |
| `catalog_cache_seconds` | `30.0` | |

### `providers.custom[]` — cualquier endpoint OpenAI-compatible

LM Studio, vLLM, NVIDIA NIM, OpenRouter… Los `id` deben ser únicos y no pueden
usar los nombres reservados `ollama`, `deepseek` ni `bootstrap`.

| Clave | Defecto | Qué hace |
|---|---|---|
| `id` | *(obligatorio)* | Solo letras, números, guion y guion bajo |
| `enabled` | `false` | |
| `adapter` | `openai_compatible` | Único valor admitido hoy |
| `display_name` | `null` | Nombre para el panel |
| `base_url` | *(obligatorio)* | |
| `timeout_seconds` | `300` (remoto) / `2400` (`deployment: local`) | Un proveedor local que no declare el suyo hereda el plazo por defecto de una tarea; un valor explícito se respeta siempre |
| `api_key_env` | `NVIDIA_API_KEY` | Ojo con el defecto heredado: en un proveedor que no sea NVIDIA, ponlo o vacíalo explícitamente |
| `keyring_service` | `ai-broker` | |
| `keyring_username` | `null` → `{id}_api_key` | Se rellena solo con el id del proveedor |
| `deployment` | `cloud` | `cloud` \| `api` \| `local`. **Es lo que decide la frontera de datos**: solo `local` cuenta como local para `local_only`/`confidential` |
| `auto_start` | `false` | Levantar el servidor local al arrancar. Hoy solo implementado para LM Studio (`lms server start`) y solo con `deployment: local`; en cualquier otro caso se ignora con un warning |
| `sync_models` | `false` | Descubrir el catálogo llamando a `/models` en vez de usar solo la lista escrita en `models` |
| `catalog_cache_seconds` | `5.0` | |
| `default_context_window` | `128000` | Ventana que se asume para un modelo descubierto sin declarar |
| `probe_max_output_tokens` | `1` | Tokens de cada petición de sondeo (1–1024) |
| `probe_delay_seconds` | `1.0` | Pausa entre sondeos, para no golpear el endpoint (0–60) |
| `probe_max_models` | `10` | Modelos por tanda de sondeo (1–1000) |
| `probe_skip_compatible` | `true` | No repetir los ya verificados como compatibles |
| `probe_skip_checked` | `true` | No repetir ningún modelo ya comprobado, aunque saliera incompatible |
| `probe_features` | `true` | Tras verificar el chat, sondear visión, JSON estructurado y tools: tres peticiones de un token por modelo operativo |
| `input_cost_per_million` / `output_cost_per_million` | `0.0` | Tarifas por defecto del proveedor; cada modelo puede fijar las suyas |
| `rescue_reasoning_content` | `true` | Usar `reasoning_content` cuando `content` llega vacío. Nunca pisa una respuesta válida y siempre queda marcado con `content_source` |
| `models` | `[]` | Catálogo declarado (ver abajo) |

Cada entrada de `models`:

| Clave | Defecto | Qué hace |
|---|---|---|
| `name` | *(obligatorio)* | Identificador tal y como lo espera el endpoint |
| `context_window` | `128000` | |
| `input_cost_per_million` / `output_cost_per_million` | `0.0` | |
| `capabilities` | `["completion"]` | `completion`, `embedding`… |
| `compatibility` | `unknown` | `compatible` \| `incompatible` \| `error`. **`incompatible` veta el modelo** (error definitivo de contrato: 400/404/422); `error` es un fallo temporal (5xx, timeout, credenciales) y se reintenta |
| `compatibility_checked_at`, `compatibility_error` | `null` | Cuándo y por qué |
| `features` | `{}` | Capacidades **verificadas por sondeo**: `vision`, `json_mode` y `tools`. Clave ausente = sin sondear; `true`/`false` = verificado. Un `false` aquí excluye al modelo aunque el catálogo externo diga lo contrario. Se respeta también `image_output` si alguien lo escribe a mano, pero hoy ningún sondeo lo mide: la única evidencia de que un modelo *genera* imágenes sale del catálogo externo o de las capacidades que declare el runtime |
| `features_checked_at` | `null` | |

Estos campos los escribe el propio broker al sondear desde la página *Modelos*:
se editan a mano solo para corregir algo que se sabe mal medido.

---

## Ajustes que hoy no hacen nada

Declarados y editables, pero sin ningún lector en el código. Se documentan aquí
para que nadie pierda una tarde moviéndolos:

| Clave | Situación |
|---|---|
| `server.workers` | Solo se valida que sea `1`; el runner no lo propaga a Uvicorn (le pasa la instancia de la app, que ya fuerza un único proceso). No es un mando suelto: es el invariante escrito donde se puede comprobar |

`server.cors_enabled`, `resources.max_loaded_local_models` y
`resources.scheduling_policy` estuvieron en esta lista hasta el 23 de agosto de
2026, declarados y sin ningún lector. Ya no: los tres hacen lo que dicen, y hay
tests que lo sostienen (`tests/test_operator_settings.py`).

---

## Ver también

- [`Phase_6_Operations.md`](Phase_6_Operations.md) — retención, logging, descarga por inactividad, backup.
- [`Phase_7_File_Ingestion.md`](Phase_7_File_Ingestion.md) — ingesta, imágenes y OCR.
- [`Phase_9_Speed_And_Lanes.md`](Phase_9_Speed_And_Lanes.md) — ranking por tiempo esperado y carriles.
- [`Client_API.md`](Client_API.md) — lo que ve una aplicación cliente.
- [`../Deployment_Guide.md`](../Deployment_Guide.md) — despliegue en Windows.
