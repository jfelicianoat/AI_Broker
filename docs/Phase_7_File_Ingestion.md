# Fase 7 — Ingesta de ficheros adjuntos

Fecha: 19 de julio de 2026

El broker acepta ficheros (documentos, imágenes, audio y vídeo), los convierte a
Markdown en segundo plano y los inyecta en el prompt de las tareas que los
referencian. El modelo destino siempre recibe texto: el mapeo es sin pérdida
respecto al contrato existente y funciona con cualquier proveedor.

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
| Imagen | `.png .jpg .jpeg .webp .tiff .tif .bmp` | Docling OCR + descripción visión |
| Audio | `.mp3 .wav .m4a .flac .ogg .opus .aac` | faster-whisper |
| Vídeo | `.mp4 .mkv .mov .avi .webm .m4v .wmv` | ffmpeg (extrae audio) + faster-whisper |

Los motores se importan en perezoso: si falta el paquete, solo ese fichero
falla (`ENGINE_MISSING` con hint `pip install "ai-broker[ingestion]"`); el
broker arranca y opera igual. La transcripción de vídeo exige además `ffmpeg`
en el PATH (o `ingestion.transcription.ffmpeg_path`).

## Descripción de figuras (documentos con gráficos)

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
  max_file_mb: 200
  max_pdf_pages: 500
  ocr_enabled: true
  ocr_languages: [es, en]
  conversion_timeout_seconds: 1800
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

Tabla `ingested_files` (id, sha256, filename, kind, engine, status,
error_json, original_path, markdown_path, meta_json). Los ficheros viven en
`state/files/{file_id}/` (`original.*` + `converted.md`). Las conversiones
interrumpidas por un reinicio se relanzan en el arranque (idempotentes).

## Panel del dashboard (2026-07-19)

Página **Ficheros** (`/dashboard/files`, nav propia): formulario de subida
(multipart + CSRF, errores renderizados en la propia página), tabla con
auto-refresco cada 5 s (fragmento HTMX `/dashboard/fragments/files`) que
muestra tipo/motor/tamaño/tokens estimados/estado, enlace "Ver Markdown" para
los `ready` y botón "Borrar" (elimina fila y directorio; avisa de que las
tareas encoladas que lo referencien fallarán).

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

## Pendiente / siguientes pasos

- Ampliar el corpus con un PDF escaneado real (OCR) y un XLSX con tablas
  cuando haya ejemplares representativos del flujo del usuario.
