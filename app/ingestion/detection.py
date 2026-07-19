"""Detección y validación de formatos de fichero admitidos por la ingesta.

La extensión decide el motor de conversión; los magic bytes verifican que el
contenido corresponde a lo que la extensión declara (un ".pdf" que no empieza
por %PDF se rechaza en la subida, no a mitad de conversión).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath


class UnsupportedFormat(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Detection:
    kind: str       # pdf | office | text | image | audio | video
    engine: str     # docling | markitdown | passthrough | whisper | whisper_video
    extension: str  # con punto, en minúsculas


# PDF nativo o escaneado: Docling decide por página si aplica OCR.
PDF_EXTENSIONS = {".pdf"}

# Formatos que MarkItDown convierte a Markdown directamente.
MARKITDOWN_EXTENSIONS = {
    ".docx", ".xlsx", ".pptx", ".epub", ".msg", ".html", ".htm", ".ipynb",
}

# Texto plano y marcado ligero: se entregan tal cual (ya son Markdown o legibles).
PLAIN_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".adoc", ".org", ".tex", ".log",
}

# Código y datos estructurados: passthrough envuelto en fence con el lenguaje.
CODE_TEXT_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".java": "java",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".cs": "csharp", ".go": "go",
    ".rs": "rust", ".rb": "ruby", ".php": "php", ".sql": "sql", ".sh": "bash",
    ".ps1": "powershell", ".bat": "batch", ".ini": "ini", ".toml": "toml",
    ".cfg": "ini", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".xml": "xml", ".csv": "csv", ".tsv": "tsv",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".bmp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv"}

TEXT_EXTENSIONS = PLAIN_TEXT_EXTENSIONS | set(CODE_TEXT_EXTENSIONS)

# Listado para /api/v1/capabilities y mensajes de rechazo.
ALLOWED_FORMATS: dict[str, list[str]] = {
    "pdf": sorted(PDF_EXTENSIONS),
    "office": sorted(MARKITDOWN_EXTENSIONS),
    "text": sorted(TEXT_EXTENSIONS),
    "image": sorted(IMAGE_EXTENSIONS),
    "audio": sorted(AUDIO_EXTENSIONS),
    "video": sorted(VIDEO_EXTENSIONS),
}


def _sniff_ok(extension: str, head: bytes) -> bool:
    """True si los magic bytes son coherentes con la extensión declarada.

    Solo se verifican firmas inequívocas; los contenedores sin firma fiable
    (mp3 sin ID3, wma...) pasan y fallarán en conversión si están corruptos.
    """
    if extension == ".pdf":
        return head.startswith(b"%PDF")
    if extension in {".docx", ".xlsx", ".pptx", ".epub"}:
        return head.startswith(b"PK\x03\x04")
    if extension == ".msg":
        return head.startswith(b"\xd0\xcf\x11\xe0")
    if extension == ".png":
        return head.startswith(b"\x89PNG")
    if extension in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if extension in {".tiff", ".tif"}:
        return head.startswith(b"II*\x00") or head.startswith(b"MM\x00*")
    if extension == ".bmp":
        return head.startswith(b"BM")
    if extension == ".wav":
        return head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    if extension == ".flac":
        return head.startswith(b"fLaC")
    if extension in {".ogg", ".opus"}:
        return head.startswith(b"OggS")
    if extension in {".mp4", ".mov", ".m4a", ".m4v"}:
        return head[4:8] == b"ftyp"
    if extension in {".mkv", ".webm"}:
        return head.startswith(b"\x1aE\xdf\xa3")
    if extension == ".avi":
        return head.startswith(b"RIFF") and head[8:12] == b"AVI "
    return True


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # latin-1/cp1252 siempre decodifica; sin NUL lo tratamos como texto.
        return True


def safe_filename(filename: str) -> str:
    """Última componente del nombre, sin rutas (defensa contra path traversal)."""
    name = PureWindowsPath(filename.replace("\x00", "")).name
    return PurePosixPath(name).name or "fichero"


def detect(filename: str, head: bytes) -> Detection:
    name = safe_filename(filename)
    extension = PurePosixPath(name.lower()).suffix
    if not extension:
        raise UnsupportedFormat("INGEST_UNSUPPORTED_FORMAT", f"'{name}' no tiene extensión reconocible")

    if extension in PDF_EXTENSIONS:
        kind, engine = "pdf", "docling"
    elif extension in MARKITDOWN_EXTENSIONS:
        kind, engine = "office", "markitdown"
    elif extension in TEXT_EXTENSIONS:
        kind, engine = "text", "passthrough"
    elif extension in IMAGE_EXTENSIONS:
        kind, engine = "image", "docling"
    elif extension in AUDIO_EXTENSIONS:
        kind, engine = "audio", "whisper"
    elif extension in VIDEO_EXTENSIONS:
        kind, engine = "video", "whisper_video"
    else:
        supported = ", ".join(sorted(ext for group in ALLOWED_FORMATS.values() for ext in group))
        raise UnsupportedFormat(
            "INGEST_UNSUPPORTED_FORMAT",
            f"Extensión '{extension}' no admitida. Formatos: {supported}",
        )

    if kind == "text":
        if not _looks_like_text(head):
            raise UnsupportedFormat(
                "INGEST_CONTENT_MISMATCH",
                f"'{name}' declara texto pero su contenido es binario",
            )
    elif not _sniff_ok(extension, head):
        raise UnsupportedFormat(
            "INGEST_CONTENT_MISMATCH",
            f"El contenido de '{name}' no corresponde al formato '{extension}'",
        )
    return Detection(kind=kind, engine=engine, extension=extension)
