"""Motores de conversión a Markdown, con imports perezosos.

Ningún motor se importa al arrancar el broker: si falta el paquete, la
conversión de ese fichero falla con EngineMissing (y un hint de instalación)
sin impedir el resto de la operación. Todas las funciones son síncronas y se
ejecutan en hilos desde IngestionService.
"""
from __future__ import annotations

import base64
import io
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import IngestionImagesConfig

# Marcador que Docling emite por cada figura al exportar con ImageRefMode.PLACEHOLDER.
IMAGE_PLACEHOLDER = "<!-- image -->"

INSTALL_HINT = 'pip install "ai-broker[ingestion]"'


class EngineMissing(RuntimeError):
    def __init__(self, engine: str, hint: str = INSTALL_HINT) -> None:
        super().__init__(f"Motor de ingesta no disponible: {engine}. Instálalo con: {hint}")
        self.engine = engine
        self.hint = hint


@dataclass
class DoclingResult:
    markdown: str
    pages: int
    ocr_enabled: bool
    # PNGs de las figuras del documento, en el mismo orden que los
    # IMAGE_PLACEHOLDER del markdown. None = figura sin imagen exportable.
    pictures: list[bytes | None] = field(default_factory=list)


def _docling_converter(ocr_enabled: bool, ocr_languages: list[str], extract_images: bool) -> Any:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as error:
        raise EngineMissing("docling") from error

    options = PdfPipelineOptions()
    options.do_ocr = ocr_enabled
    options.generate_picture_images = extract_images
    options.images_scale = 2.0
    try:
        options.ocr_options.lang = list(ocr_languages)
    except Exception:
        # Algunos backends OCR no aceptan lista de idiomas; el default sirve.
        pass
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def _export_docling(document: Any, extract_images: bool) -> DoclingResult:
    try:
        from docling_core.types.doc import ImageRefMode
        markdown = document.export_to_markdown(image_mode=ImageRefMode.PLACEHOLDER)
    except Exception:
        markdown = document.export_to_markdown()
    pictures: list[bytes | None] = []
    if extract_images:
        for picture in getattr(document, "pictures", []) or []:
            png: bytes | None = None
            try:
                pil_image = picture.get_image(document)
                if pil_image is not None:
                    buffer = io.BytesIO()
                    pil_image.save(buffer, format="PNG")
                    png = buffer.getvalue()
            except Exception:
                png = None
            pictures.append(png)
    pages = len(getattr(document, "pages", {}) or {})
    return DoclingResult(markdown=markdown, pages=pages, ocr_enabled=False, pictures=pictures)


def convert_pdf_docling(
    path: Path,
    *,
    ocr_enabled: bool,
    ocr_languages: list[str],
    max_pages: int,
    extract_images: bool,
) -> DoclingResult:
    """PDF a Markdown. Docling decide por página si hace falta OCR (escaneos)."""
    converter = _docling_converter(ocr_enabled, ocr_languages, extract_images)
    conversion = converter.convert(str(path), max_num_pages=max_pages)
    result = _export_docling(conversion.document, extract_images)
    result.ocr_enabled = ocr_enabled
    return result


def convert_image_docling(path: Path, *, ocr_languages: list[str]) -> str:
    """OCR de un fichero de imagen suelto; devuelve el texto reconocido en Markdown."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as error:
        raise EngineMissing("docling") from error
    conversion = DocumentConverter().convert(str(path))
    return conversion.document.export_to_markdown()


def convert_with_markitdown(path: Path) -> str:
    """Office/EPUB/HTML/IPYNB/MSG a Markdown via MarkItDown."""
    try:
        from markitdown import MarkItDown
    except ImportError as error:
        raise EngineMissing("markitdown") from error
    result = MarkItDown(enable_plugins=False).convert(str(path))
    markdown = getattr(result, "markdown", None) or getattr(result, "text_content", "")
    return str(markdown)


_whisper_cache: dict[tuple[str, str], Any] = {}


def transcribe_audio(
    path: Path,
    *,
    model_size: str,
    device: str,
    language: str | None,
) -> tuple[str, dict[str, Any]]:
    """Audio a Markdown con marcas de tiempo por segmento (faster-whisper)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise EngineMissing("faster-whisper") from error

    cache_key = (model_size, device)
    model = _whisper_cache.get(cache_key)
    if model is None:
        model = WhisperModel(model_size, device=device, compute_type="auto")
        _whisper_cache[cache_key] = model

    segments, info = model.transcribe(str(path), language=language, vad_filter=True)
    lines: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        minutes, seconds = divmod(int(segment.start), 60)
        lines.append(f"**[{minutes:02d}:{seconds:02d}]** {text}")
    meta = {
        "language": getattr(info, "language", None),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
        "duration_seconds": round(float(getattr(info, "duration", 0.0) or 0.0), 1),
        "whisper_model": model_size,
    }
    return "\n\n".join(lines), meta


def extract_audio_ffmpeg(video_path: Path, output_wav: Path, *, ffmpeg_path: str) -> None:
    """Extrae la pista de audio de un vídeo a WAV mono 16 kHz (óptimo para Whisper)."""
    command = [
        ffmpeg_path, "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(output_wav),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=1800, check=False,
        )
    except FileNotFoundError as error:
        raise EngineMissing(
            "ffmpeg",
            "instala ffmpeg y añádelo al PATH o configura ingestion.transcription.ffmpeg_path",
        ) from error
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg fallo (exit {completed.returncode}): {stderr}")


_DESCRIBE_PROMPT = (
    "Describe de forma pormenorizada esta figura extraída de un documento. "
    "Si es un gráfico o diagrama: indica su tipo, ejes, series, unidades, tendencias "
    "y valores aproximados relevantes. Si es una tabla o captura: transcribe su "
    "contenido esencial. Responde solo con la descripción, en el idioma del contexto. "
    "Contexto del documento alrededor de la figura:\n\n"
)


def describe_image_openai(
    config: IngestionImagesConfig,
    png_bytes: bytes,
    context_text: str,
    api_key: str | None,
) -> str:
    """Descripción de una figura con un LLM de visión (endpoint OpenAI-compatible)."""
    import httpx

    encoded = base64.b64encode(png_bytes).decode("ascii")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": config.model,
        "temperature": 0.2,
        "max_tokens": 600,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DESCRIBE_PROMPT + context_text[:1200]},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
    }
    response = httpx.post(
        config.base_url.rstrip("/") + "/chat/completions",
        json=payload,
        headers=headers,
        timeout=config.timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    return str(content or "").strip()
