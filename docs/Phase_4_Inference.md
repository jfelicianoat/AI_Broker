# Fase 4 — Inferencia transparente y resultados

## Estado

Implementada y verificada el 2026-06-24.

## Contrato neutral

`TaskCreateRequest.inference_kind` admite `chat` —valor predeterminado— y `embedding`. Embedding requiere estrategia `single`, salida `json`, Ollama local y un modelo con capacidad `embedding`.

El input es `content.prompt`. El Broker lo transmite como un único mensaje `user` o como `input` de `/api/embed`, sin reescribirlo. Los attachments se rechazan mientras no exista un mapeo lossless; nunca se ignoran silenciosamente.

Desde la incorporación del servicio de compresión de prompts ([`Prompt_Compression.md`](Prompt_Compression.md)), la transmisión sin reescritura aplica cuando `prompt_compression.enabled` es `false`. Con el servicio activo, el prompt de chat se comprime antes del envío al proveedor; `content.prompt` persiste intacto y los embeddings nunca se comprimen.

## Traducción

- Ollama chat usa `/api/chat`, conserva el prompt y traduce temperatura/límite de salida. Para JSON pasa el schema mediante `format`.
- Ollama embedding usa `/api/embed` con `truncate: false` y acepta un único vector numérico finito.
- DeepSeek usa `/chat/completions`; para JSON solicita `response_format: json_object`. Este adapter no anuncia embeddings.

El Broker no parsea ni valida el JSON o Markdown de negocio devuelto.

## Contexto

El preflight usa la ventana descubierta y una cota conservadora: bytes UTF-8 de entrada, schema cuando aplica, `max_output_tokens` y margen de plantilla para chat. Puede rechazar antes que un tokenizer exacto, pero garantiza que no se trunca silenciosamente.

Si el modelo preferido no cabe, el router usa otro permitido solo cuando `fallback_allowed=true`; en caso contrario devuelve `CONTEXT_LIMIT_EXCEEDED`. Una ventana desconocida produce `CONTEXT_WINDOW_UNKNOWN`.

## Resultado y durabilidad

Chat devuelve `assistant_content` sin interpretarlo y conserva `result_markdown` por compatibilidad. Embedding devuelve `embedding`. Ambos incluyen `inference_kind`, `output_format`, `usage`, `model_used`, `models_used` y `fallback_used`.

En estrategia `single`, la invocación y el resultado terminal se confirman en una misma transacción SQLite. Los artefactos son auditoría auxiliar y un fallo al escribirlos no invalida el resultado confirmado.

## Verificación

Siete pruebas específicas cubren contrato, attachments, traducción exacta, JSON opaco, contexto sin truncado, fallback, embeddings y persistencia. La regresión completa suma 32 pruebas superadas. Se verificó además chat real con `granite4.1:3b`, respuesta `OK`, métricas y descarga. No había un modelo embedding instalado para un smoke real; `/api/embed` está cubierto con transporte HTTP simulado.
