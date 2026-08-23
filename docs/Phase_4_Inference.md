# Fase 4 — Inferencia transparente y resultados

## Estado

Implementada y verificada el 2026-06-24.

## Contrato neutral

`TaskCreateRequest.inference_kind` admite `chat` —valor predeterminado— y `embedding`. Embedding requiere estrategia `single`, salida `json`, Ollama local y un modelo con capacidad `embedding`.

El input es `content.prompt`. El Broker lo transmite como un único mensaje `user` o como `input` de `/api/embed`, sin reescribirlo. En `single` no se añade ningún mensaje `system`; las estrategias que sí lo llevan y el texto literal de cada uno están en [`System_Prompts.md`](System_Prompts.md). Los attachments sin mapeo lossless se rechazan; nunca se ignoran silenciosamente. Desde la fase 7 existe un mapeo lossless para ficheros ingeridos (`type: "broker_file"`): el documento convertido a Markdown se inyecta en el prompt en el despacho, conservando `request_json` con el prompt original (ver [`Phase_7_File_Ingestion.md`](Phase_7_File_Ingestion.md)).

Desde la incorporación del servicio de compresión de prompts ([`Prompt_Compression.md`](Prompt_Compression.md)), la transmisión sin reescritura aplica cuando `prompt_compression.enabled` es `false`. Con el servicio activo, el prompt de chat se comprime antes del envío al proveedor; `content.prompt` persiste intacto y los embeddings nunca se comprimen.

## Traducción

- Ollama chat usa `/api/chat`, conserva el prompt y traduce temperatura/límite de salida. Para JSON pasa el schema mediante `format`.
- Ollama embedding usa `/api/embed` con `truncate: false` y acepta un único vector numérico finito.
- DeepSeek usa `/chat/completions`; para JSON solicita `response_format: json_object`. Este adapter no anuncia embeddings.

El Broker no parsea ni valida el JSON o Markdown de negocio devuelto.

**Multimodalidad (agosto 2026).** Con imágenes adjuntas, el mensaje de usuario deja de ser texto plano y cada adapter lo escribe en su dialecto: Ollama las lleva en un campo `images` del propio mensaje (base64 suelto), los OpenAI-compatibles como lista de partes `text` + `image_url` con data URI. Mandar el formato del otro **no da error**: da una respuesta inventada, que es el peor fallo posible. Sin imágenes, el contenido sigue siendo la cadena de siempre, porque hay servidores compatibles que solo aceptan esa forma. Los bytes viajan en `inline_images`, que se excluye de toda serialización (`request_json`, artefactos, telemetría, estado agéntico persistido): la imagen ya está guardada una vez en la ingesta, y duplicarla en cada volcado serían megabytes de base64 por tarea. Al reanudar un bucle agéntico se readjuntan desde la petición, que se expande de nuevo.

**Respuestas con imagen.** Si el modelo devuelve imágenes (base64 suelto o data URI, según el dialecto), se guardan como artefactos `image_output` de la tarea. Una respuesta **solo** con imagen no es una respuesta vacía: en vez de `INVALID_PROVIDER_RESPONSE`, el contenido pasa a ser un texto que remite a los artefactos, porque el contrato exige contenido y "no ha contestado" sería falso. Una imagen ofrecida como URL remota se ignora: descargarla sería tráfico de salida que la tarea no ha autorizado.

## Contexto

El preflight usa la ventana descubierta y una cota conservadora: bytes UTF-8 de entrada, schema cuando aplica, `max_output_tokens` y margen de plantilla para chat. Puede rechazar antes que un tokenizer exacto, pero garantiza que no se trunca silenciosamente.

Si el modelo preferido no cabe, el router usa otro permitido solo cuando `fallback_allowed=true`; en caso contrario devuelve `CONTEXT_LIMIT_EXCEEDED`. Una ventana desconocida produce `CONTEXT_WINDOW_UNKNOWN`.

## Resultado y durabilidad

Chat devuelve `assistant_content` sin interpretarlo y conserva `result_markdown` por compatibilidad. Embedding devuelve `embedding`. Ambos incluyen `inference_kind`, `output_format`, `usage`, `model_used`, `models_used` y `fallback_used`.

En estrategia `single`, la invocación y el resultado terminal se confirman en una misma transacción SQLite. Los artefactos son auditoría auxiliar y un fallo al escribirlos no invalida el resultado confirmado.

## Verificación

Siete pruebas específicas cubren contrato, attachments, traducción exacta, JSON opaco, contexto sin truncado, fallback, embeddings y persistencia. La regresión completa suma 32 pruebas superadas. Se verificó además chat real con `granite4.1:3b`, respuesta `OK`, métricas y descarga. No había un modelo embedding instalado para un smoke real; `/api/embed` está cubierto con transporte HTTP simulado.
