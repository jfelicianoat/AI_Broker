"""Fase pesada de la conversión: fichero → Markdown, sin red ni base de datos.

Es la mitad del trabajo que **puede matarse**: Docling con OCR, MarkItDown,
ffmpeg y Whisper tardan minutos y consumen la máquina. Vive separada de
`app.ingestion.service` por dos motivos concretos:

1. La ejecuta un proceso hijo (`app.ingestion.worker`), y un proceso hijo sí se
   puede abortar de verdad. `asyncio.to_thread`, que es lo que se usaba antes,
   NO es interrumpible: cancelar la corrutina dejaba el hilo de Docling
   corriendo hasta el final, comiendo CPU por detrás.
2. Aísla los fallos catastróficos. Docling arrastra torch; un OOM dentro del
   hijo mata al hijo, no al broker.

Lo que NO hace: describir figuras con un modelo de visión. Eso necesita el
proveedor, las credenciales y escribir en SQLite, así que se queda en el padre
(ver `IngestionService._describe_pictures`). Aquí las figuras se dejan escritas
en disco con su marcador en el Markdown.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import IngestionConfig
from app.ingestion import engines
from app.ingestion.detection import CODE_TEXT_EXTENSIONS

# Nombre del subdirectorio donde el hijo deja las figuras extraídas del PDF.
FIGURES_DIRNAME = "figures"


@dataclass
class ConversionInput:
    """Lo mínimo del fichero que la conversión necesita saber.

    Deliberadamente un puñado de campos planos y no el FileRecord entero: este
    objeto cruza la frontera de proceso como JSON."""

    file_id: str
    filename: str
    extension: str
    kind: str
    original_path: str
    # Política de imágenes YA resuelta para este fichero (ver
    # IngestionService.store_upload_from_file). Viaja con la conversión porque
    # el hijo no lee la configuración del broker, y porque decidirlo por
    # fichero es justo el punto: extraer las figuras de un PDF grande cuesta
    # tanto como describirlas, y hay documentos donde no aportan nada.
    describe_images: bool = False

    @classmethod
    def from_record(cls, record: Any) -> ConversionInput:
        return cls(
            file_id=record.id,
            filename=record.filename,
            extension=record.extension,
            kind=record.kind,
            original_path=record.original_path,
            describe_images=bool(getattr(record, "describe_images", False)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "extension": self.extension,
            "kind": self.kind,
            "original_path": self.original_path,
            "describe_images": self.describe_images,
        }


@dataclass
class ConversionResult:
    markdown: str
    meta: dict[str, Any] = field(default_factory=dict)
    # Rutas de las figuras extraídas, en el mismo orden que sus marcadores en
    # el Markdown. La descripción con el modelo de visión la hace el padre.
    figures: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"markdown": self.markdown, "meta": self.meta, "figures": self.figures}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ConversionResult:
        return cls(
            markdown=str(payload.get("markdown") or ""),
            meta=dict(payload.get("meta") or {}),
            figures=[str(item) for item in payload.get("figures") or []],
        )


# Callback de progreso: el hijo lo usa para publicar datos que deben verse
# ANTES de terminar (la duración de un vídeo de tres horas, por ejemplo).
ProgressFn = Callable[[dict[str, Any]], None]


def _noop(_: dict[str, Any]) -> None:
    return None


def convert_file(
    source: ConversionInput,
    settings: IngestionConfig,
    on_progress: ProgressFn = _noop,
) -> ConversionResult:
    """Convierte el fichero a Markdown. Sincrónica y sin efectos externos más
    allá de escribir las figuras junto al original."""
    path = Path(source.original_path)
    if source.kind == "pdf":
        return _convert_pdf(source, path, settings)
    if source.kind == "office":
        return ConversionResult(engines.convert_with_markitdown(path), {"engine": "markitdown"})
    if source.kind == "text":
        return _convert_text(source, path)
    if source.kind == "image":
        return _convert_image(source, path, settings)
    if source.kind == "audio":
        return _transcribe(path, settings, on_progress)
    if source.kind == "video":
        return _convert_video(path, settings, on_progress)
    raise RuntimeError(f"Tipo de fichero desconocido: {source.kind}")


def _convert_pdf(
    source: ConversionInput, path: Path, settings: IngestionConfig,
) -> ConversionResult:
    result = engines.convert_pdf_docling(
        path,
        ocr_enabled=settings.ocr_enabled,
        ocr_languages=settings.ocr_languages,
        max_pages=settings.max_pdf_pages,
        extract_images=source.describe_images,
    )
    figures = _write_figures(path.parent, result.pictures)
    meta: dict[str, Any] = {
        "engine": "docling",
        "pages": result.pages,
        "ocr": result.ocr_enabled,
        "pictures": len(result.pictures),
    }
    return ConversionResult(result.markdown, meta, figures)


def _write_figures(file_dir: Path, pictures: list[bytes | None]) -> list[str]:
    """Vuelca las figuras a disco para que el padre pueda describirlas.

    Cruzan la frontera de proceso como ficheros y no como bytes en el JSON:
    un PDF con cien imágenes convertiría la salida del hijo en decenas de MB
    de base64."""
    if not pictures:
        return []
    figures_dir = file_dir / FIGURES_DIRNAME
    figures_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for index, png in enumerate(pictures):
        if png is None:
            # Hueco: el marcador existe pero no hay imagen. Se conserva la
            # posición con una cadena vacía para no descuadrar los índices.
            written.append("")
            continue
        target = figures_dir / f"{index:04d}.png"
        target.write_bytes(png)
        written.append(str(target))
    return written


def _convert_text(source: ConversionInput, path: Path) -> ConversionResult:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    language = CODE_TEXT_EXTENSIONS.get(source.extension)
    if language is not None:
        fence = "````" if "```" in text else "```"
        text = f"{fence}{language}\n{text}\n{fence}"
    return ConversionResult(text, {"engine": "passthrough"})


def _convert_image(
    source: ConversionInput, path: Path, settings: IngestionConfig
) -> ConversionResult:
    """Solo el OCR. La descripción con visión la añade el padre, que es quien
    tiene proveedor: aquí se deja anotado que hay una imagen que describir."""
    meta: dict[str, Any] = {"engine": "docling", "ocr": settings.ocr_enabled}
    parts: list[str] = []
    if settings.ocr_enabled:
        ocr_text = engines.convert_image_docling(path, ocr_languages=settings.ocr_languages)
        if ocr_text.strip():
            parts.append(f"**Texto reconocido (OCR):**\n\n{ocr_text}")
    return ConversionResult(
        "\n\n".join(parts), meta, [str(path)] if source.describe_images else []
    )


def _probe_duration(
    path: Path, settings: IngestionConfig, on_progress: ProgressFn,
) -> float | None:
    duration = engines.probe_media_duration(
        path, ffmpeg_path=settings.transcription.ffmpeg_path,
    )
    if duration is not None:
        # Se publica antes de transcribir: la duración de un audio de horas
        # debe verse en el panel mientras el trabajo sigue en marcha.
        on_progress({"duration_seconds": round(duration, 1)})
    return duration


def _require_transcription(settings: IngestionConfig) -> None:
    if not settings.transcription.enabled:
        raise engines.EngineMissing(
            "transcription", "activa ingestion.transcription.enabled en broker_config.yaml",
        )


def _transcribe(
    path: Path, settings: IngestionConfig, on_progress: ProgressFn,
) -> ConversionResult:
    _require_transcription(settings)
    transcription = settings.transcription
    probed = _probe_duration(path, settings, on_progress)
    text, meta = engines.transcribe_audio(
        path,
        model_size=transcription.model_size,
        device=transcription.device,
        language=transcription.language,
    )
    meta["engine"] = "whisper"
    if probed is not None:
        # ffprobe mide el contenedor completo; el valor de whisper puede
        # quedarse corto si el VAD recorta silencio final.
        meta["duration_seconds"] = round(probed, 1)
    return ConversionResult(text, meta)


def _convert_video(
    path: Path, settings: IngestionConfig, on_progress: ProgressFn,
) -> ConversionResult:
    _require_transcription(settings)
    transcription = settings.transcription
    probed = _probe_duration(path, settings, on_progress)
    wav_path = path.parent / "audio.wav"
    engines.extract_audio_ffmpeg(
        path, wav_path,
        ffmpeg_path=transcription.ffmpeg_path,
        timeout_seconds=settings.conversion_timeout_seconds,
    )
    try:
        text, meta = engines.transcribe_audio(
            wav_path,
            model_size=transcription.model_size,
            device=transcription.device,
            language=transcription.language,
        )
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass
    meta["engine"] = "ffmpeg+whisper"
    if probed is not None:
        meta["duration_seconds"] = round(probed, 1)
    return ConversionResult(text, meta)


def load_settings(payload: dict[str, Any]) -> IngestionConfig:
    return IngestionConfig.model_validate(payload)


def dump_settings(settings: IngestionConfig) -> dict[str, Any]:
    return json.loads(settings.model_dump_json())
