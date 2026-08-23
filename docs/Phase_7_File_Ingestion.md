# Fase 7 — Ingesta de ficheros adjuntos

Fecha: 19 de julio de 2026

El broker acepta ficheros (documentos, imágenes, audio y vídeo). Los documentos
—y el audio y el vídeo— se convierten a Markdown en segundo plano y se inyectan
en el prompt de las tareas que los referencian. **Las imágenes sueltas no se
convierten**: se adjuntan tal cual y las mira un modelo con visión, que el
enrutado exige (ver *Imágenes adjuntas*, más abajo).

## Flujo

```
POST /api/v1/files  (multipart)          POST /api/v1/tasks
        │                                        │ attachments:
        ▼                                        │   - type: broker_file
  received ──► converting ──► ready ◄────────────┘     metadata: {file_id}
                    │                            (409 si no está ready)
                    ▼                                    │ despacho
                  failed                                 ▼
                                            prompt + <attached_document>…
```

1. `POST /api/v1/files` valida extensión, magic bytes y tamaño; deduplica por
   SHA-256 (re-subir un fichero ya convertido devuelve el mismo `file_id` sin
   repetir OCR). Responde `202` con `status_url`.
2. La conversión corre como tarea asyncio (hilo aparte); el cliente sondea
   `GET /api/v1/files/{id}` hasta `ready` (o `failed` con código de error).
3. La tarea adjunta con `content.attachments[].type = "broker_file"` y
   `metadata.file_id` (o `uri: broker://files/{id}`). La creación falla rápido
   con `ATTACHED_FILE_NOT_FOUND / _NOT_READY / _FAILED` si procede.
4. En el despacho, el coordinador expande el prompt: el Markdown de cada
   adjunto se añade dentro de `<attached_document id name>` con una
   advertencia de que es contenido no confiable (datos, no instrucciones) y
   de posibles errores de OCR. El `request_json` persistido conserva el
   prompt original del cliente; la expansión es reproducible en reintentos.
5. Con adjuntos, la compresión de prompt pasa a `off` salvo override explícito
   de la tarea: la compresión caveman corrompería tablas y código.

## Formatos y motores

| Tipo | Extensiones | Motor |
|---|---|---|
| PDF (nativo o escaneado) | `.pdf` | Docling (OCR por página con EasyOCR) |
| Office/eBook/HTML | `.docx .xlsx .pptx .epub .msg .html .htm .ipynb` | MarkItDown |
| Texto y marcado | `.txt .md .rst .adoc .org .tex .log` | passthrough |
| Código y datos | `.py .js .ts .java .c .cpp .cs .go .rs .rb .php .sql .sh .ps1 .bat .ini .toml .cfg .yaml .yml .json .xml .csv .tsv` | passthrough en fence |
| Imagen | `.png .jpg .jpeg .webp .tiff .tif .bmp` | ninguno: se adjunta sin convertir (OCR solo a petición) |
| Audio | `.mp3 .wav .m4a .flac .ogg .opus .aac` | faster-whisper |
| Vídeo | `.mp4 .mkv .mov .avi .webm .m4v .wmv` | ffmpeg (extrae audio) + faster-whisper |

Los motores se importan en perezoso: si falta el paquete, solo ese fichero
falla (`ENGINE_MISSING` con hint `pip install "ai-broker[ingestion]"`); el
broker arranca y opera igual. La transcripción de vídeo exige además `ffmpeg`
en el PATH (o `ingestion.transcription.ffmpeg_path`).

## Imágenes adjuntas

Una imagen suelta ya no pasa por Docling. Antes se le hacía OCR y se le pedía a
un modelo de visión un párrafo que la describiera, y ese párrafo —no la imagen—
era lo que llegaba al modelo que atendía la tarea: todo lo que la descripción no
mencionara se perdía, sin que nadie lo notara. Ahora:

- Al subirla queda `ready` directamente (no hay Markdown que esperar, así que
  tampoco entra en el carril de conversión ni tiene `markdown_url`). La única
  excepción es pedir OCR (más abajo).
- **TIFF y BMP se reescriben a PNG** al subirlos. Ningún endpoint de visión los
  acepta, así que si viajaran tal cual la tarea moriría con un error del
  proveedor *después* de esperar en la cola. Es un cambio de envoltorio, sin
  pérdida, y se paga una vez: `extension` pasa a `.png`, el nombre original se
  conserva y `meta.transcoded_from` deja constancia. De un TIFF multipágina se
  guarda el primer fotograma. Requiere Pillow (viene con los extras de
  ingesta); sin él la subida se rechaza con `INGEST_ENGINE_MISSING` en vez de
  fallar más tarde.
- En el despacho, `expand_request` mete en el prompt un manifiesto
  `<attached_image id name orden>` —qué es, en qué orden va— y los bytes viajan
  aparte, en `inline_images`, hasta el adapter del proveedor: Ollama los recibe
  en `images: [b64]`; los OpenAI-compatibles, como `image_url` con data URI.
  Mandar el formato del otro no da error, da una respuesta inventada.
- `inline_images` no es parte del contrato público: una petición que lo traiga
  se rechaza. La única puerta de entrada de bytes es la ingesta, que valida
  formato, magic bytes y tamaño.

### Capacidad exigida al modelo

Con imágenes en la tarea, `eligible_catalog` descarta todo modelo sin visión, y
si no queda ninguno la tarea falla con `VISION_MODEL_UNAVAILABLE` y un mensaje
que lo explica. La evidencia de "ve imágenes" está en `app.model_capabilities` y
sigue la jerarquía de siempre: sondeo contra el endpoint > catálogo externo
(models.dev) > nada. Un negativo verificado excluye aunque models.dev afirme lo
contrario.

### OCR: el texto que hay DENTRO de la imagen

Adjuntar la imagen y describirla es una cosa; extraer lo que pone en ella es
otra, y el broker la sabe hacer **sin modelo de visión**. Hay dos caminos:

1. **Pedido al subir** — `POST /api/v1/files` con `ocr=true` (solo tiene efecto
   en imágenes). Esa imagen sí pasa por el carril de conversión: nace
   `received`, y al terminar su `markdown_url` sirve el texto reconocido.
   `meta.ocr_chars` dice cuánto se reconoció. La imagen original se conserva
   intacta — el OCR es un añadido, nunca un reemplazo — y al adjuntarla a una
   tarea viajan **las dos cosas**: la imagen (para quien pueda verla) y un
   bloque `<attached_image_text>` con la transcripción literal.
   El OCR forma parte de la identidad de la subida para la dedupe por SHA-256,
   con la misma regla `>=` que `describe_images`: la subida *con* texto sirve a
   quien lo pide *sin* él, pero no al revés.
2. **Al vuelo, como último recurso** — si la petición va de leer lo que pone en
   la imagen (`classify_ocr_intent`: "extrae el texto", "qué pone en", "hazle un
   OCR", "transcribe"…) y **no hay ningún modelo con visión** disponible para
   esa tarea, la expansión corre el OCR en ese momento y la imagen entra como
   texto en vez de como imagen. Es peor que verla, pero es una respuesta donde
   antes había un `VISION_MODEL_UNAVAILABLE`.

La degradación se limita a ese caso. Con un "¿de qué color es el fondo?" y sin
modelo de visión, la tarea **sigue fallando**: un OCR no contesta esa pregunta, y
cambiar la imagen por su texto sería fingir que se ha atendido la petición. El
coordinador consulta `RoutedModelProvider.has_vision_model(request)` antes de
expandir; si no puede responder, asume que sí hay visión (equivocarse por
optimismo solo lleva al mensaje de error correcto, equivocarse por pesimismo
degradaría una tarea que se podía atender bien).

El motor es el mismo Docling que ya hace el OCR de los PDF escaneados, sin LLM
de por medio: de eso va pedir OCR, de sacar lo que pone y no de que alguien lo
interprete.

### Peticiones que piden una imagen de salida

`app.task_classifier.classify_image_intent` detecta si la petición pide *generar*
una imagen ("genera una imagen de…", "dibújame…") o *modificar* la adjunta
("recorta la foto"). En ese caso solo son candidatos los modelos que producen
imágenes (`modalities.output` con `image` en models.dev); si no hay ninguno, la
tarea falla con `IMAGE_GENERATION_UNSUPPORTED` diciéndolo, en vez de entregar la
descripción del cartel en lugar del cartel.

El clasificador es deliberadamente estrecho: un falso positivo tumba una tarea
que el broker sabía atender ("resume la imagen adjunta" no pide una imagen
nueva), y ese daño es peor que el de un falso negativo. Quedan fuera a propósito
"logo", "icono" y "diagrama": se piden tanto como imagen como en formatos que un
modelo de texto sí escribe (SVG, Mermaid).

Cuando el modelo sí devuelve una imagen (base64 en `message.images`, en
cualquiera de los dos dialectos), se guarda como artefacto de la tarea
(`image_output`), no en el resultado JSON: ese documento se lee entero en cada
consulta del estado. Se recoge por `GET /api/v1/tasks/{id}/artifacts` (listado
con tipo MIME, tamaño y SHA-256) y su `download_url`; el detalle de la tarea en
el panel la enseña, además de enumerarla.

## Descripción de figuras (documentos con gráficos)

Esto sigue vigente y es otra cosa: aquí la figura está **embebida** en un
documento cuyo resto es texto, y tiene que ocupar su sitio en él.

Con `ingestion.images.enabled`, las figuras que Docling extrae de un PDF se
envían una a una a un LLM de visión (endpoint OpenAI-compatible; configurado
apunta a LM Studio con `google/gemma-4-31b-qat`). El prompt incluye el texto
adyacente a la figura para anclar la descripción al documento, y la respuesta
sustituye al marcador en su posición original:

```markdown
> **[Figura 3 — descripción generada por IA]:** Gráfico de barras que compara…
```

Figuras por encima de `max_images` o con error de descripción quedan marcadas
como `[Figura N: imagen no descrita]` sin abortar la conversión.

`ingestion.images.enabled` es solo el **valor por defecto**: cada subida puede
decidirlo con `describe_images` (campo del formulario en `POST /api/v1/files`,
desplegable en el panel de Ficheros). Omitirlo hereda la configuración; `true` y
`false` mandan sobre ella para ese fichero. Con `false` no se paga nada de esto:
Docling no renderiza las imágenes (`extract_images=false`), no se busca modelo de
visión y no se hace ninguna llamada — que es donde está el ahorro en documentos
grandes cuyas figuras no aportan información.

La política se guarda **ya resuelta** en `ingested_files.describe_images` y entra
en la deduplicación por SHA-256: una conversión con descripciones sirve para
quien las pide sin ellas, pero no al revés. En `meta.images_described` queda
constancia de que un Markdown se generó sin describir las figuras, que no es lo
mismo que un documento que no tenía ninguna.

## Seguridad

- Magic bytes verificados contra la extensión declarada (`INGEST_CONTENT_MISMATCH`).
- Nombres de fichero saneados (sin componentes de ruta).
- Límite de tamaño (`max_file_mb`), de páginas PDF (`max_pdf_pages`) y timeout
  de conversión (`conversion_timeout_seconds`).
- Anti-inyección: el contenido del documento no puede cerrar su propio tag
  `<attached_document>` (mismo patrón que los delimitadores del árbitro) y el
  prompt marca el bloque como datos, nunca instrucciones.
- Subidas y lecturas de Markdown exigen credencial admin cuando hay token.

## Configuración (`broker_config.yaml`)

```yaml
ingestion:
  enabled: true
  storage_dir: state/files
  max_file_mb: 8192        # subida en streaming a disco: el tope no toca RAM
  max_pdf_pages: 2000
  ocr_enabled: true
  ocr_languages: [es, en]
  conversion_timeout_seconds: 7200   # 2 h: audio de varias horas en CPU
  images:
    enabled: true
    base_url: http://127.0.0.1:1234/v1   # LM Studio
    model: google/gemma-4-31b-qat        # visión verificada por sondeo
    timeout_seconds: 180.0
    max_images: 20
  transcription:
    enabled: true
    model_size: small                    # faster-whisper: tiny/small/medium/large-v3
    device: auto
    language: null                       # null = autodetección
    ffmpeg_path: ffmpeg
```

## Persistencia

Tabla `ingested_files` (id, sha256, filename, extension, kind, engine,
size_bytes, status, error_json, original_path, markdown_path, meta_json,
`describe_images`, `ocr`). Las dos últimas son columnas añadidas por migración
(`ALTER TABLE` idempotente en `app.db`) y entran en la deduplicación por
SHA-256: describen *qué* conversión se guardó, no solo de qué fichero venía.
Las filas anteriores a cada columna valen `NULL` y se asumen convertidas con la
política global vigente, que es la mejor aproximación disponible.

Los ficheros viven en `state/files/{file_id}/` (`original.*` + `converted.md`).
Una imagen adjuntada sin OCR **no tiene `converted.md`** ni `markdown_path`: su
directorio guarda solo el original, y su `markdown_url` viene a `null` tanto en
la API como en el panel — enlazarlo daría un 409 al primer clic.

Las conversiones interrumpidas por un reinicio se relanzan en el arranque
(idempotentes). Una imagen que quedara en `received` de una versión anterior a
este cambio no vuelve a Docling: al reanudar se marca `ready` con motor
`attach`, porque ya no es un documento que convertir.

## Panel del dashboard (2026-07-19)

Página **Ficheros** (`/dashboard/files`, nav propia): formulario de subida
(multipart + CSRF, errores renderizados en la propia página), tabla con
auto-refresco cada 5 s (fragmento HTMX `/dashboard/fragments/files`) que
muestra tipo/motor/tamaño/tokens estimados/estado, enlace "Ver Markdown" para
los `ready` **que tengan Markdown**, enlace "Ver imagen" para las imágenes
(`/dashboard/files/{id}/original`: sin Markdown que enseñar, su fila no ofrecía
nada que mirar) y botón "Borrar" (elimina fila y directorio; avisa de que las
tareas encoladas que lo referencien fallarán).

El formulario tiene dos controles de política, y conviene no confundirlos
porque suenan parecido y gobiernan cosas distintas:

- **Desplegable "figuras del documento"** (`describe_images`) — las figuras
  *embebidas* en un PDF o un DOCX. No toca a las imágenes sueltas.
- **Casilla "Reconocer el texto de la imagen (OCR)"** (`ocr`) — solo para
  imágenes sueltas. Sin marcar, la imagen se adjunta tal cual y la mira un
  modelo con visión; marcada, además se extrae el texto que haya dentro y
  queda disponible como Markdown.

El botón dice "Subir" y no "Subir y convertir": con una imagen no hay nada que
convertir, y prometerlo dejaba al usuario esperando un estado que no iba a
cambiar.

## Retención y estimación de tokens (2026-07-19)

- `persistence.files_retention_days` (0 = nunca borrar, igual que los
  artefactos): `prune_ingested_files` en el arranque poda ficheros `ready`/
  `failed` más antiguos que el umbral (fila + directorio). Las conversiones en
  curso jamás se podan.
- `meta.tokens_estimate` en `GET /api/v1/files/{id}`: cota superior
  conservadora del Markdown (misma fórmula que el enrutado por contexto), para
  elegir modelo/estrategia sin descargar el documento.

## Probador con adjuntos (2026-07-19)

El probador de prompts lista los ficheros `ready` como casillas (nombre, tipo
y tokens estimados) junto al selector de compresión. Las casillas viajan como
`attach_file_<file_id>` y `_build_prompt_tester_request` las convierte en
`attachments type=broker_file`. Mismo fail-fast que la API: un adjunto no
listo devuelve el error `ATTACHED_FILE_NOT_READY` en la página sin encolar.

## Corpus dorado (2026-07-19)

`tests/fixtures/ingestion/` (DOCX, PDF nativo, Markdown, CSV — generados a
mano, sin binarios opacos) + `tests/test_ingestion_corpus.py`: cada caso sube
el fichero real y verifica por sub-cadenas que el Markdown conserva el
contenido clave (los formatos pueden variar entre versiones de motor; perder
contenido es la regresión). Los casos con motor pesado se saltan si el paquete
no está instalado; el caso PDF/Docling es opt-in con `AI_BROKER_CORPUS_PDF=1`
(verificado en local: pasa con Docling real).

## Ficheros grandes y duración (2026-07-19)

No hay límite en minutos: los límites reales son tamaño de subida y timeout
de conversión, ambos ampliados para la máquina del usuario (64 GB RAM):

- **Subida en streaming**: `stream_upload_to_temp` vuelca el multipart a
  `state/files/incoming/` por chunks de 1 MB, corta en cuanto supera
  `max_file_mb` (sin drenar el resto del stream) y el hash SHA-256 de dedupe
  se calcula también en streaming — un vídeo de gigabytes nunca pasa por RAM.
  `store_upload_from_file` consume siempre el temporal (movido con
  `os.replace` al almacén, mismo volumen, o borrado); los temporales
  huérfanos de un crash se limpian en el arranque (`cleanup_incoming`).
- **Timeout de ffmpeg**: sigue a `conversion_timeout_seconds` (antes 1800 s
  fijos). Whisper no impone límite de duración (procesa por segmentos con
  memoria constante); con `small` en CPU rinde ~4–8× tiempo real, así que el
  plazo de 2 h cubre audios de ~8 h o más.
- **Duración en meta**: al iniciar la transcripción, ffprobe (derivado de
  `ffmpeg_path`) mide el contenedor y `meta.duration_seconds` se publica
  ANTES de terminar la conversión (visible en el panel junto al tamaño);
  falla en blando (sin ffprobe → sin dato, la transcripción no depende de él).
- Equivalencias orientativas del tope de 8 GB: ~10 h de vídeo de reunión
  (~1,5 Mbps), días de audio comprimido, ~13 h de WAV sin comprimir.
- El límite práctico posterior es la ventana de contexto del modelo destino:
  una transcripción de 3 h ronda 30–45k tokens (`meta.tokens_estimate` lo
  anticipa; el preflight falla explícito, nunca trunca).

## Map-reduce de contexto largo (2026-07-19)

Resuelto el límite práctico posterior: con `execution.long_context:
"map_reduce"` (opt-in por tarea, contrato 2.5+ — el default sigue fallando
explícito, el broker jamás trocea en silencio), si los documentos exceden el
contexto de todos los modelos elegibles, el coordinador divide la sección de
documentos del prompt expandido (split por el centinela compartido
`ATTACHED_DOCS_SENTINEL`), procesa cada fragmento con la instrucción íntegra
(rol `chunk_map`, estado `chunking`) y sintetiza (rol `chunk_reduce`, con
reducción jerárquica hasta 4 rondas si las parciales no caben juntas).
Presupuesto verificado entre invocaciones, cancelación entre fragmentos,
eventos `chunking.planned`/`chunking.completed` y desglose en
`result.long_context`. Detalle del contrato en
[`../Agent_AI_Broker.md`](../Agent_AI_Broker.md) (Novedades 2.5).

## Pendiente / siguientes pasos

- Ampliar el corpus con un PDF escaneado real (OCR) y un XLSX con tablas
  cuando haya ejemplares representativos del flujo del usuario.
