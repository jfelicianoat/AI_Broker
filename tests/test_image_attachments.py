"""Imágenes adjuntas: se adjuntan, no se convierten; y quien las atiende ve.

Tres reglas se prueban aquí, y las tres existen porque el fallo contrario es
silencioso:

1. Una imagen suelta ya no pasa por Docling. Antes llegaba al modelo un párrafo
   *sobre* la imagen escrito por otro modelo, y todo lo que esa descripción no
   mencionara se perdía sin que nadie lo notara.
2. Si la tarea lleva imágenes, el modelo que la atienda tiene que verlas. Un
   modelo solo-texto ante una imagen no da error: contesta a partir del nombre
   del fichero como si la hubiera mirado.
3. Si la tarea pide una imagen y nadie sabe producirla, se dice. La alternativa
   era entregar la descripción del cartel en vez del cartel.
"""
import asyncio
import base64
import io
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import BrokerConfig, IngestionConfig, PersistenceConfig, ProcessingConfig
from app.db import Database
from app.ingestion.detection import detect
from app.ingestion.service import IngestionService
from app.providers import RoutedModelProvider
from app.providers.base import ProviderError, returned_images
from app.schemas import InlineImage, TaskCreateRequest, requires_vision
from app.task_classifier import classify_image_intent, classify_ocr_intent

# PNG de 1x1 píxel: lo mínimo que pasa la comprobación de magic bytes.
PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def service(tmp_path: Path) -> IngestionService:
    db = Database(tmp_path / "broker.db")
    db.init_schema()
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        ingestion=IngestionConfig(storage_dir=str(tmp_path / "files"), isolate_conversions=False),
    )
    yield IngestionService(db, config)
    db.close()


def _task(prompt: str, file_ids: list[str] | None = None) -> TaskCreateRequest:
    return TaskCreateRequest(
        idempotency_key=f"image-test-{time.monotonic_ns()}",
        content={
            "prompt": prompt,
            "attachments": [
                {"type": "broker_file", "metadata": {"file_id": file_id}}
                for file_id in file_ids or []
            ],
        },
    )


# ------------------------------------------------------- la imagen no se convierte

def test_an_image_is_attached_not_converted() -> None:
    detection = detect("foto.png", PIXEL_PNG)
    assert detection.kind == "image"
    assert detection.engine == "attach"


def test_an_uploaded_image_is_ready_without_any_conversion(service: IngestionService) -> None:
    record, created = service.store_upload("foto.png", PIXEL_PNG)
    assert created
    # Listo desde el primer momento: no hay Markdown que esperar, así que el
    # cliente puede crear la tarea sin sondear un estado que no iba a cambiar.
    assert record.status == "ready"
    assert record.markdown_path is None
    assert record.meta["converted"] is False


def test_an_image_never_reaches_the_conversion_lane(service: IngestionService) -> None:
    record, _ = service.store_upload("foto.png", PIXEL_PNG)

    class _Repository:
        def ingestion_task_for_file(self, file_id):  # pragma: no cover - no debe llamarse
            raise AssertionError("una imagen no tiene conversión que encolar")

        def create_ingestion_task(self, *args):  # pragma: no cover - no debe llamarse
            raise AssertionError("una imagen no tiene conversión que encolar")

    assert service.enqueue(_Repository(), record.id) is None


def test_the_prompt_gets_a_manifest_and_the_image_travels_apart(service: IngestionService) -> None:
    record, _ = service.store_upload("gato.png", PIXEL_PNG)
    expanded = service.expand_request(_task("¿Qué animal es?", [record.id]))

    # El manifiesto dice qué está mirando y en qué orden...
    assert '<attached_image id="' in expanded.content.prompt
    assert "gato.png" in expanded.content.prompt
    # ...pero el contenido de la imagen NO está en el prompt.
    assert base64.b64encode(PIXEL_PNG).decode("ascii") not in expanded.content.prompt
    # Viaja aparte, en su propio campo, listo para el proveedor.
    assert len(expanded.inline_images) == 1
    image = expanded.inline_images[0]
    assert image.media_type == "image/png"
    assert base64.b64decode(image.data_base64) == PIXEL_PNG
    assert requires_vision(expanded)


def test_a_document_still_travels_as_text(service: IngestionService) -> None:
    record, _ = service.store_upload("nota.md", b"# Titulo\n\nCuerpo del documento")
    service.db.execute(
        "UPDATE ingested_files SET status = 'ready', markdown_path = ? WHERE id = ?",
        (str(Path(record.original_path)), record.id),
    )
    expanded = service.expand_request(_task("Resume", [record.id]))
    assert "Cuerpo del documento" in expanded.content.prompt
    assert not expanded.inline_images


def test_a_client_cannot_inject_images_into_a_request() -> None:
    """La única puerta de entrada de bytes es la ingesta, que valida formato y
    tamaño. `inline_images` lo rellena el broker al expandir, y punto."""
    with pytest.raises(ValueError, match="inline_images"):
        TaskCreateRequest(
            idempotency_key="image-test-injected",
            content={"prompt": "mira esto"},
            inline_images=[{
                "file_id": "file_x", "filename": "x.png",
                "media_type": "image/png", "data_base64": "AAAA",
            }],
        )


# ------------------------------------------------------------ intención de imagen

@pytest.mark.parametrize("prompt", [
    "genera una imagen de un gato con sombrero",
    "dibújame un plano de la casa",
    "hazme una ilustración para la portada",
    "create an image of a red car",
])
def test_asking_for_a_new_image_is_detected(prompt: str) -> None:
    assert classify_image_intent(_task(prompt)) == "generate"


@pytest.mark.parametrize("prompt", [
    "haz un resumen de la imagen adjunta",
    "describe la imagen",
    "extrae el texto de la imagen",
    "crea una descripción de la imagen",
    "genera un diagrama de flujo del proceso",
    "modifica el código según la imagen adjunta",
])
def test_talking_about_an_image_is_not_asking_for_one(prompt: str) -> None:
    """El falso positivo es el error caro: tumbaría una tarea que el broker
    sabe atender. Estos prompts hablan de una imagen; ninguno pide otra."""
    assert classify_image_intent(_task(prompt)) is None


def test_editing_needs_an_image_to_edit(service: IngestionService) -> None:
    record, _ = service.store_upload("foto.png", PIXEL_PNG)
    expanded = service.expand_request(_task("recorta la foto y quita el fondo", [record.id]))
    assert classify_image_intent(expanded) == "edit"
    # Sin la imagen delante no hay nada que editar, y tampoco es un encargo de
    # dibujar: sale como tarea normal para que el modelo pida el adjunto que
    # falta, en vez de morir con un error sobre modelos de imagen.
    assert classify_image_intent(_task("recorta la foto y quita el fondo")) is None


# --------------------------------------------------------------------- enrutado

class _CatalogStub:
    def __init__(self, models):
        self._models = models

    async def models(self):
        return self._models

    async def close(self):
        return None


def _entry(name: str, **extra) -> dict:
    return {
        "name": name,
        "provider": "ollama",
        "deployment": "local",
        "context_window": 100000,
        "capabilities": ["completion"],
        "compatibility": "compatible",
        **extra,
    }


def _router(catalog: list[dict]) -> RoutedModelProvider:
    return RoutedModelProvider(
        BrokerConfig(), ollama=_CatalogStub(catalog), deepseek=_CatalogStub([]),
    )


def _with_image(prompt: str) -> TaskCreateRequest:
    request = _task(prompt)
    # model_copy no revalida: hay que pasar el objeto ya construido, igual que
    # hace expand_request.
    return request.model_copy(update={"inline_images": [InlineImage(
        file_id=f"file_{uuid4().hex}", filename="foto.png", media_type="image/png",
        data_base64=base64.b64encode(PIXEL_PNG).decode("ascii"),
    )]})


def test_a_task_with_images_only_goes_to_a_model_that_sees() -> None:
    router = _router([
        _entry("solo-texto"),
        _entry("con-vision", features={"vision": True}),
    ])
    selected = asyncio.run(router.select(_with_image("¿qué se ve aquí?"), 1, ["single"]))
    assert selected[0].model == "con-vision"


def test_a_verified_negative_beats_the_external_catalog() -> None:
    """El sondeo manda sobre models.dev: si el endpoint dijo que no ve, no ve."""
    router = _router([
        _entry("desmentido", features={"vision": False}, catalog={"vision": True}),
        _entry("declarado", catalog={"vision": True}),
    ])
    selected = asyncio.run(router.select(_with_image("¿qué se ve aquí?"), 1, ["single"]))
    assert selected[0].model == "declarado"


def test_without_any_vision_model_the_task_is_told_so() -> None:
    router = _router([_entry("solo-texto"), _entry("tampoco")])
    with pytest.raises(ProviderError) as error:
        asyncio.run(router.select(_with_image("¿qué se ve aquí?"), 1, ["single"]))
    assert error.value.code == "VISION_MODEL_UNAVAILABLE"
    assert "ningún modelo disponible" in str(error.value)


def test_asking_for_an_image_without_a_generator_is_told_so() -> None:
    router = _router([_entry("solo-texto", features={"vision": True})])
    with pytest.raises(ProviderError) as error:
        asyncio.run(router.select(_task("genera una imagen de un gato"), 1, ["single"]))
    assert error.value.code == "IMAGE_GENERATION_UNSUPPORTED"
    assert "generar" in str(error.value)


def test_asking_for_an_image_goes_to_the_model_that_makes_them() -> None:
    router = _router([
        _entry("solo-texto"),
        _entry("pintor", catalog={"image_output": True}),
    ])
    selected = asyncio.run(router.select(_task("genera una imagen de un gato"), 1, ["single"]))
    assert selected[0].model == "pintor"


def test_a_plain_task_is_not_forced_through_the_image_filters() -> None:
    router = _router([_entry("solo-texto"), _entry("otro")])
    selected = asyncio.run(router.select(_task("resume esto"), 1, ["single"]))
    assert selected[0].model in {"solo-texto", "otro"}


# ------------------------------------------------------- respuestas con imagen

def test_an_image_answer_is_understood_in_both_transports() -> None:
    """Ollama devuelve base64 suelto; los OpenAI-compatibles, un data URI."""
    images = returned_images({"images": [
        base64.b64encode(PIXEL_PNG).decode("ascii"),
        {"image_url": {"url": "data:image/jpeg;base64,QUJD"}},
    ]})
    assert [image.media_type for image in images] == ["image/png", "image/jpeg"]
    assert images[0].extension == ".png" and images[1].extension == ".jpg"


def test_a_remote_url_is_not_fetched() -> None:
    """Descargarla sería tráfico de salida que la tarea no ha autorizado."""
    assert returned_images({"images": [{"image_url": {"url": "https://example.com/x.png"}}]}) == ()


# ------------------------------------------------- cada proveedor, su dialecto

def _image_request(prompt: str = "¿qué ves?") -> TaskCreateRequest:
    return _with_image(prompt)


def test_ollama_gets_the_image_in_its_own_field() -> None:
    """Ollama quiere `images: [b64]` en el mensaje; el formato de OpenAI no le
    da un error, le da una respuesta inventada a partir del texto."""
    import httpx

    from app.providers.ollama import OllamaProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "vl", "size": 1, "context_length": 8192,
                 "capabilities": ["completion", "vision"]},
            ]})
        if request.url.path == "/api/chat":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "message": {"content": "un gato"}, "prompt_eval_count": 3, "eval_count": 2,
            })
        # /api/ps y /api/generate: el gestor de VRAM los consulta alrededor de
        # cada inferencia.
        return httpx.Response(200, json={"models": []})

    provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
    request = _image_request()
    output = asyncio.run(provider.generate(request, "vl", "¿qué ves?"))
    asyncio.run(provider.close())

    user = seen["body"]["messages"][-1]
    assert user["images"] == [request.inline_images[0].data_base64]
    assert isinstance(user["content"], str)
    assert output.content == "un gato"


def test_an_openai_compatible_endpoint_gets_a_data_uri() -> None:
    import httpx

    from app.config import OpenAICompatibleProviderConfig
    from app.providers.openai_compatible import OpenAICompatibleProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "vl"}]})
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "un gato"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    provider = OpenAICompatibleProvider(
        OpenAICompatibleProviderConfig(
            id="lmstudio", enabled=True, base_url="http://127.0.0.1:1234/v1",
            deployment="local", sync_models=True,
        ),
        transport=httpx.MockTransport(handler),
    )
    request = _image_request()
    asyncio.run(provider.generate(request, "vl", "¿qué ves?"))
    asyncio.run(provider.close())

    parts = seen["body"]["messages"][-1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_text_only_task_keeps_the_plain_string_content() -> None:
    """Sin imágenes, el mensaje sigue siendo una cadena: hay servidores
    compatibles que solo aceptan esa forma."""
    import httpx

    from app.config import OpenAICompatibleProviderConfig
    from app.providers.openai_compatible import OpenAICompatibleProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    provider = OpenAICompatibleProvider(
        OpenAICompatibleProviderConfig(
            id="lmstudio", enabled=True, base_url="http://127.0.0.1:1234/v1",
            deployment="local", sync_models=True,
        ),
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(provider.generate(_task("hola"), "m", "hola"))
    asyncio.run(provider.close())
    assert seen["body"]["messages"][-1]["content"] == "hola"


def test_an_answer_that_is_only_an_image_is_not_an_empty_answer() -> None:
    import httpx

    from app.providers.ollama import OllamaProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "pintor", "size": 1, "context_length": 8192,
                 "capabilities": ["completion"]},
            ]})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={
                "message": {"content": "", "images": [base64.b64encode(PIXEL_PNG).decode("ascii")]},
                "prompt_eval_count": 3, "eval_count": 2,
            })
        return httpx.Response(200, json={"models": []})

    provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
    output = asyncio.run(provider.generate(_task("dibuja un gato"), "pintor", "dibuja un gato"))
    asyncio.run(provider.close())

    assert len(output.images) == 1
    assert base64.b64decode(output.images[0].data_base64) == PIXEL_PNG
    assert output.content  # el contrato exige texto: se pone uno que dice qué pasó


def test_a_returned_image_is_stored_as_an_artifact(tmp_path: Path) -> None:
    """No va en el resultado JSON: ese documento se lee entero en cada consulta
    del estado, y un PNG en base64 dentro lo haría pesar megabytes."""
    from app.artifacts import ArtifactStore
    from app.providers.base import GeneratedImage

    store = ArtifactStore(tmp_path)
    image = GeneratedImage(media_type="image/png", data_base64=base64.b64encode(PIXEL_PNG).decode("ascii"))
    record = store.write_bytes("task_1", f"single/image_01{image.extension}", PIXEL_PNG)
    assert Path(record.path).read_bytes() == PIXEL_PNG
    assert record.size_bytes == len(PIXEL_PNG)


# ------------------------------------------------------ formatos que nadie mira

def _image_bytes(mode: str = "RGB", fmt: str = "TIFF") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 30)).convert(mode).save(buffer, format=fmt)
    return buffer.getvalue()


def test_a_tiff_is_stored_as_png(service: IngestionService) -> None:
    """Ningún endpoint de visión acepta TIFF ni BMP: si llegaran tal cual, la
    tarea moriría con un error del proveedor después de esperar en la cola."""
    record, _ = service.store_upload("escaneo.tiff", _image_bytes())
    assert record.extension == ".png"
    assert Path(record.original_path).read_bytes()[:4] == b"\x89PNG"
    assert record.meta["transcoded_from"] == ".tiff"
    # El nombre con el que lo subieron se conserva: lo que cambia es el
    # envoltorio, no el fichero que el usuario cree tener.
    assert record.filename == "escaneo.tiff"


def test_a_bmp_with_a_palette_survives_the_transcode(service: IngestionService) -> None:
    record, _ = service.store_upload("captura.bmp", _image_bytes(fmt="BMP"))
    assert record.extension == ".png"
    assert service.inline_image(record).media_type == "image/png"


def test_a_png_is_left_exactly_as_it_arrived(service: IngestionService) -> None:
    record, _ = service.store_upload("foto.png", PIXEL_PNG)
    assert record.extension == ".png"
    assert Path(record.original_path).read_bytes() == PIXEL_PNG
    assert "transcoded_from" not in record.meta


# --------------------------------------------------------------------- OCR

@pytest.mark.parametrize("prompt", [
    "extrae el texto de esta imagen",
    "hazle un OCR",
    "¿qué pone en la foto?",
    "transcribe lo que se lee",
    "read the text in the image",
])
def test_asking_for_the_text_inside_an_image_is_detected(prompt: str) -> None:
    assert classify_ocr_intent(_task(prompt)) is True


@pytest.mark.parametrize("prompt", [
    "describe la escena",
    "¿de qué color es el gato?",
    "genera una imagen de un gato",
])
def test_other_questions_about_an_image_are_not_ocr(prompt: str) -> None:
    assert classify_ocr_intent(_task(prompt)) is False


def test_asking_for_ocr_on_upload_sends_the_image_to_the_lane(service: IngestionService) -> None:
    """Es lo único que hace que una imagen se convierta: hay trabajo de verdad
    que hacer y puede tardar, así que no puede nacer `ready`."""
    record, _ = service.store_upload_from_file(
        "recibo.png", _temp_with(service, PIXEL_PNG), None, True,
    )
    assert record.ocr is True
    assert record.status == "received"
    assert record.engine == "ocr"


def test_the_same_image_with_and_without_ocr_are_not_the_same_upload(
    service: IngestionService,
) -> None:
    first, created_first = service.store_upload("recibo.png", PIXEL_PNG)
    second, created_second = service.store_upload_from_file(
        "recibo.png", _temp_with(service, PIXEL_PNG), None, True,
    )
    assert created_first and created_second
    assert first.id != second.id
    # Al revés sí se reutiliza: la que trae texto sirve para quien no lo pide.
    third, created_third = service.store_upload("recibo.png", PIXEL_PNG)
    assert not created_third


def test_recognized_text_travels_with_the_image(service: IngestionService) -> None:
    record = _with_recognized_text(service, "TOTAL: 42,00 EUR")
    expanded = service.expand_request(_task("¿cuál es el total?", [record.id]))
    # La imagen sigue yendo entera —el OCR no la sustituye—, y el texto se
    # añade porque quien la subió lo pidió.
    assert len(expanded.inline_images) == 1
    assert "<attached_image_text" in expanded.content.prompt
    assert "TOTAL: 42,00 EUR" in expanded.content.prompt


def test_without_a_vision_model_the_text_answers_instead_of_failing(
    service: IngestionService,
) -> None:
    """El caso que motivó todo esto: la app manda una foto y pide el texto, y
    en el broker no hay ningún modelo con visión. Antes: tarea muerta."""
    record = _with_recognized_text(service, "TOTAL: 42,00 EUR")
    expanded = service.expand_request(
        _task("extrae el texto de la imagen", [record.id]), vision_available=False,
    )
    assert not expanded.inline_images          # ya no hace falta que nadie mire
    assert not requires_vision(expanded)       # …así que el enrutado no lo exige
    assert "TOTAL: 42,00 EUR" in expanded.content.prompt


def test_without_a_vision_model_a_visual_question_still_fails(
    service: IngestionService,
) -> None:
    """Un OCR no contesta de qué color es un gato: degradar aquí sería fingir
    que se ha atendido la petición."""
    record = _with_recognized_text(service, "TOTAL: 42,00 EUR")
    expanded = service.expand_request(
        _task("¿de qué color es el fondo?", [record.id]), vision_available=False,
    )
    assert requires_vision(expanded)


def test_the_fallback_runs_ocr_when_nobody_asked_for_it_on_upload(
    service: IngestionService, monkeypatch,
) -> None:
    record, _ = service.store_upload("recibo.png", PIXEL_PNG)
    monkeypatch.setattr(
        "app.ingestion.engines.convert_image_docling",
        lambda path, ocr_languages: "  texto reconocido al vuelo  ",
    )
    expanded = service.expand_request(
        _task("extrae el texto de la imagen", [record.id]), vision_available=False,
    )
    assert not expanded.inline_images
    assert "texto reconocido al vuelo" in expanded.content.prompt


def test_a_failing_ocr_leaves_the_image_and_the_clear_error(
    service: IngestionService, monkeypatch,
) -> None:
    """Si el OCR no está disponible, la tarea vuelve a necesitar visión y el
    enrutado dirá exactamente eso; lo que no puede es fallar por sorpresa."""
    record, _ = service.store_upload("recibo.png", PIXEL_PNG)

    def _boom(path, ocr_languages):
        raise RuntimeError("docling no instalado")

    monkeypatch.setattr("app.ingestion.engines.convert_image_docling", _boom)
    expanded = service.expand_request(
        _task("extrae el texto de la imagen", [record.id]), vision_available=False,
    )
    assert requires_vision(expanded)


def _temp_with(service: IngestionService, data: bytes) -> Path:
    service.incoming_dir.mkdir(parents=True, exist_ok=True)
    temp = service.incoming_dir / f"upload-{uuid4().hex}.tmp"
    temp.write_bytes(data)
    return temp


def _with_recognized_text(service: IngestionService, text: str):
    """Una imagen subida con `ocr=true` cuya conversión ya terminó."""
    record, _ = service.store_upload_from_file(
        "recibo.png", _temp_with(service, PIXEL_PNG), None, True,
    )
    markdown = Path(record.original_path).parent / "converted.md"
    markdown.write_text(text, encoding="utf-8")
    service.db.execute(
        "UPDATE ingested_files SET status = 'ready', markdown_path = ? WHERE id = ?",
        (str(markdown), record.id),
    )
    updated = service.get(record.id)
    assert updated is not None
    return updated


def test_the_lane_turns_an_ocr_upload_into_its_text(
    service: IngestionService, monkeypatch,
) -> None:
    """El extremo que ve la app: sube la imagen pidiendo OCR y, cuando la
    conversión termina, `markdown` es el texto que había dentro."""
    record, _ = service.store_upload_from_file(
        "recibo.png", _temp_with(service, PIXEL_PNG), None, True,
    )
    monkeypatch.setattr(
        "app.ingestion.engines.convert_image_docling",
        lambda path, ocr_languages: "TOTAL: 42,00 EUR\nIVA: 7,29 EUR",
    )
    asyncio.run(service.process(record.id))

    updated = service.get(record.id)
    assert updated is not None and updated.status == "ready"
    assert service.markdown(record.id) == "TOTAL: 42,00 EUR\nIVA: 7,29 EUR"
    assert updated.meta["ocr"] is True
    # La imagen original sigue ahí: el texto es un añadido, no un reemplazo.
    assert Path(updated.original_path).read_bytes() == PIXEL_PNG


def test_the_api_accepts_the_ocr_request_and_reports_it(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "api.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        ingestion=IngestionConfig(
            storage_dir=str(tmp_path / "files"), isolate_conversions=False,
        ),
    )
    with TestClient(create_app(config)) as client:
        plain = client.post("/api/v1/files", files={"file": ("foto.png", PIXEL_PNG)}).json()
        # Sin OCR no hay nada que esperar: la app puede crear la tarea ya.
        assert plain["status"] == "ready"
        assert plain["ocr"] is False
        assert client.get(f"/api/v1/files/{plain['file_id']}").json()["markdown_url"] is None

        asked = client.post(
            "/api/v1/files",
            files={"file": ("recibo.png", PIXEL_PNG)},
            data={"ocr": "true"},
        ).json()
        assert asked["ocr"] is True
        assert asked["status"] == "received"   # hay conversión que hacer
        assert asked["file_id"] != plain["file_id"]
