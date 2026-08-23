# Fase 3 — Providers y enrutamiento

## Estado

Completada el 2026-06-23.

> **Nota histórica (julio 2026):** el proveedor `huggingface_local` mencionado en este documento se retiró por redundante con LM Studio (proveedor `custom` OpenAI-compatible que sirve GGUF locales en `http://127.0.0.1:1234/v1`). Los proveedores vigentes son `ollama`, `deepseek` y los `custom` OpenAI-compatible; `local_only` acepta como locales `ollama` y `lmstudio`. El resto del flujo descrito aquí sigue siendo válido.

## Flujo operativo

1. El router obtiene el catálogo habilitado de Ollama, Hugging Face local y, opcionalmente, DeepSeek.
2. Filtra por `allowed_providers`, prioriza `preferred_model` y respeta `fallback_allowed`.
3. Un semáforo global garantiza una sola llamada LLM simultánea en todo el Broker.
4. Para Ollama, el lifecycle manager comprueba `/api/ps`, reserva capacidad y evita descargar modelos con lease.
5. La llamada usa `keep_alive: -1`; con `processing.unload_after_task` el bloque `finally` envía `keep_alive: 0` y confirma la descarga antes de liberar el slot. Con ese ajuste desactivado el modelo se queda cargado —dos tareas seguidas con el mismo modelo dejan de pagar la recarga— y quien devuelve la memoria es el bucle de inactividad (`processing.idle_unload_seconds`), bajo el mismo lock que la admisión y solo con la máquina de verdad parada: ver [`Phase_6_Operations.md`](Phase_6_Operations.md).
6. La respuesta normaliza contenido, tokens, coste y latencia. Los errores se persisten con código y `retryable`.

## Catálogo

`GET /api/v1/models` se alimenta de:

- Ollama `/api/tags`: nombre, familia, tamaño, parámetros y cuantización.
- Ollama `/api/show`: capacidades y ventana de contexto.
- Hugging Face local `providers.huggingface_local.models`: nombre, ruta en disco, capacidades y ventana de contexto declarada.
- DeepSeek `/models`: modelos accesibles para la credencial configurada.

No existen listas de modelos hardcodeadas. Si Ollama, Hugging Face local o DeepSeek no responde, su health check queda `unavailable`; SQLite sigue determinando readiness para que la API pueda aceptar trabajo en cola durante una caída temporal del proveedor.

## Seguridad y coste

- DeepSeek está deshabilitado por defecto.
- Hugging Face local está deshabilitado por defecto y requiere instalar dependencias opcionales `torch` y `transformers`.
- La clave se resuelve desde `DEEPSEEK_API_KEY` o `keyring`; no se persiste en configuración, SQLite, artefactos ni logs.
- `local_only` conserva únicamente modelos locales de Ollama y `huggingface_local`; las etiquetas Ollama con `remote_host` quedan clasificadas como deployment `cloud` y se excluyen.
- DeepSeek estima el coste máximo antes de enviar la petición y el coordinador comprueba también el coste acumulado real.
- Las tarifas son configuración operativa: deben actualizarse antes de habilitar DeepSeek.
- El análisis de compatibilidad de proveedores OpenAI-compatible avanza por tandas acotadas. Con `probe_skip_checked=true`, no repite modelos ya comprobados aunque sean incompatibles.
- Los modelos sincronizados cuyo nombre indica embeddings se prueban contra `/embeddings` y pueden atender tareas `inference_kind=embedding` si responden con un vector válido.
- Los modelos de parseo, reranking u otro uso especializado no se prueban contra `/chat/completions`; se clasifican por capacidad para evitar falsos fallos y tráfico innecesario hasta que exista un contrato de ejecución específico.
- Con `probe_features` (por defecto activo), tras verificar el chat se sondean además visión, JSON estructurado y tools: tres peticiones de un token por modelo operativo. El resultado se persiste en `features` con su fecha, y es la evidencia de más peso del broker — un negativo verificado excluye al modelo aunque models.dev afirme lo contrario (`app.model_capabilities`).

## Respuestas que llegan por el campo equivocado

`rescue_reasoning_content` (por proveedor OpenAI-compatible, activo por defecto) usa `reasoning_content` como respuesta **solo** cuando `content` llega vacío. Hay modelos —y proveedores enteros, según cómo separen el razonamiento— que entregan la contestación completa por ese campo: el proveedor responde `200`, cobra los tokens y su propia interfaz enseña el texto, mientras el broker daba la tarea por fallida por una respuesta que existía y ya estaba pagada.

El rescate **nunca pisa** un contenido válido, y siempre queda marcado: `content_source: "reasoning_content"` en la telemetría de la invocación. Un rescate silencioso haría que un modelo mal empaquetado pasara por sano, que es justo lo contrario de lo que hace falta al evaluarlo. Se apaga por proveedor para poder exigir contenido limpio en una tanda de medición sin cambiar el comportamiento del resto.

## Errores tipados

Los códigos principales son `PROVIDER_UNAVAILABLE`, `MODEL_UNAVAILABLE`, `MODEL_ERROR`, `INVALID_PROVIDER_RESPONSE`, `CREDENTIALS_UNAVAILABLE`, `LOCAL_RUNTIME_UNAVAILABLE`, `BUDGET_EXCEEDED`, `VRAM_INSUFFICIENT`, `MODEL_UNLOAD_FAILED` y `TASK_CANCELLED`.

## Verificación

- Ocho pruebas unitarias cubren discovery, contexto/capacidades, inferencia, descarga, cancelación, credenciales, coste, routing, fallback, aislamiento local/cloud y serialización global.
- Las cinco pruebas de contrato continúan pasando.
- Se verificó una inferencia real con `granite4.1:3b`: 16 tokens de entrada, 2 de salida, respuesta `OK` y descarga confirmada mediante `/api/ps`.
