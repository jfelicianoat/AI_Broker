"""Política de imágenes por fichero en la ingesta.

Describir las figuras de un documento es la parte cara de la conversión —una
llamada a un modelo de visión por figura—, y hay documentos donde no aporta
nada. Estas pruebas cubren el contrato que permite decidirlo por subida.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BrokerConfig, IngestionConfig, IngestionImagesConfig, PersistenceConfig
from app.db import Database
from app.ingestion.conversion import ConversionInput
from app.ingestion.service import IngestionService
from tests.test_ingestion import make_client, wait_for_file


def _service(tmp_path: Path, *, images_enabled: bool) -> IngestionService:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        ingestion=IngestionConfig(
            storage_dir=str(tmp_path / "files"),
            isolate_conversions=False,
            images=IngestionImagesConfig(enabled=images_enabled, model="visor"),
        ),
    )
    db = Database(tmp_path / "broker.db")
    db.init_schema()
    return IngestionService(db, config)


def _upload(service: IngestionService, name: str, body: bytes, describe: bool | None):
    incoming = service.incoming_dir
    incoming.mkdir(parents=True, exist_ok=True)
    temp = incoming / f"tmp_{name}"
    temp.write_bytes(body)
    return service.store_upload_from_file(name, temp, describe)


class TestPolicyResolution:
    @pytest.mark.parametrize("global_default", [True, False])
    def test_none_inherits_the_broker_setting(self, tmp_path, global_default: bool) -> None:
        service = _service(tmp_path, images_enabled=global_default)
        assert service.resolve_describe_images(None) is global_default

    @pytest.mark.parametrize("global_default", [True, False])
    def test_an_explicit_choice_wins_over_the_broker_setting(
        self, tmp_path, global_default: bool
    ) -> None:
        service = _service(tmp_path, images_enabled=global_default)
        assert service.resolve_describe_images(True) is True
        assert service.resolve_describe_images(False) is False

    def test_the_resolved_policy_is_stored_not_the_inherit(self, tmp_path) -> None:
        """Se guarda ya resuelta: si se guardara "heredar", cambiar la
        configuración reescribiría el significado de una conversión hecha."""
        service = _service(tmp_path, images_enabled=True)
        record, created = _upload(service, "nota.txt", b"contenido", None)
        assert created and record.describe_images is True
        service.config.ingestion.images.enabled = False
        assert service.get(record.id).describe_images is True


class TestDeduplication:
    def test_same_file_same_policy_is_reused(self, tmp_path) -> None:
        service = _service(tmp_path, images_enabled=False)
        first, created_first = _upload(service, "a.txt", b"mismo contenido", False)
        second, created_second = _upload(service, "b.txt", b"mismo contenido", False)
        assert created_first is True and created_second is False
        assert second.id == first.id

    def test_asking_for_descriptions_reconverts_what_was_stored_without_them(
        self, tmp_path
    ) -> None:
        service = _service(tmp_path, images_enabled=False)
        first, _ = _upload(service, "a.txt", b"mismo contenido", False)
        second, created = _upload(service, "a.txt", b"mismo contenido", True)
        assert created is True
        assert second.id != first.id
        assert second.describe_images is True

    def test_a_conversion_with_descriptions_serves_someone_who_asks_without(
        self, tmp_path
    ) -> None:
        """Contiene todo lo que tendría la otra y algo más: se reutiliza."""
        service = _service(tmp_path, images_enabled=True)
        first, _ = _upload(service, "a.txt", b"mismo contenido", True)
        second, created = _upload(service, "a.txt", b"mismo contenido", False)
        assert created is False
        assert second.id == first.id


class TestPolicyReachesTheConversion:
    def test_the_flag_travels_to_the_child_process(self, tmp_path) -> None:
        """El hijo no lee la configuración del broker: la política viaja con la
        entrada de conversión, que cruza la frontera de proceso como JSON."""
        service = _service(tmp_path, images_enabled=False)
        record, _ = _upload(service, "a.txt", b"contenido", True)
        source = ConversionInput.from_record(record)
        assert source.describe_images is True
        assert ConversionInput(**source.to_json()).describe_images is True


class TestNothingIsPaidForWhenImagesAreOff:
    """Donde de verdad está el ahorro: ni se extraen las figuras ni se llama al
    modelo de visión. Las dos cosas cuestan, y en un PDF grande son la
    diferencia entre segundos y una espera larguísima."""

    @staticmethod
    def _doubles(monkeypatch, seen: dict) -> None:
        import app.ingestion.engines as engines

        def fake_pdf(*args, **kwargs):
            seen["extract_images"] = kwargs.get("extract_images")
            return engines.DoclingResult(
                markdown=f"Antes\n\n{engines.IMAGE_PLACEHOLDER}\n\nDespués",
                pages=1,
                ocr_enabled=True,
                pictures=[b"fake-png"],
            )

        def fake_describe(*args, **kwargs):
            seen["described"] = seen.get("described", 0) + 1
            raise AssertionError("no debería describirse ninguna figura")

        monkeypatch.setattr(engines, "convert_pdf_docling", fake_pdf)
        monkeypatch.setattr(engines, "describe_image", fake_describe)

    def test_a_pdf_with_figures_skips_extraction_and_vision(self, tmp_path, monkeypatch) -> None:
        seen: dict = {}
        self._doubles(monkeypatch, seen)
        images = IngestionImagesConfig(enabled=True, model="vision-test")
        with make_client(tmp_path, images=images) as client:
            body = client.post(
                "/api/v1/files",
                files={"file": ("informe.pdf", b"%PDF-1.7 fake", "application/pdf")},
                data={"describe_images": "false"},
            ).json()
            state = wait_for_file(client, body["file_id"])
        assert state["status"] == "ready"
        assert seen["extract_images"] is False
        assert "described" not in seen
        # El marcador del documento sigue ahí: un Markdown sin descripciones no
        # es lo mismo que un documento sin figuras.
        assert state["meta"]["images_described"] is False
        assert "pictures_described" not in state["meta"]


class TestUploadContract:
    def test_the_api_reports_the_policy_it_will_use(self, tmp_path) -> None:
        with make_client(tmp_path, images=IngestionImagesConfig(enabled=False)) as client:
            sin = client.post(
                "/api/v1/files",
                files={"file": ("a.txt", b"contenido a")},
                data={"describe_images": "false"},
            ).json()
            con = client.post(
                "/api/v1/files",
                files={"file": ("b.txt", b"contenido b")},
                data={"describe_images": "true"},
            ).json()
            heredado = client.post(
                "/api/v1/files", files={"file": ("c.txt", b"contenido c")},
            ).json()
        assert sin["describe_images"] is False
        assert con["describe_images"] is True
        assert heredado["describe_images"] is False

    def test_the_file_state_exposes_the_policy(self, tmp_path) -> None:
        with make_client(tmp_path, images=IngestionImagesConfig(enabled=False)) as client:
            body = client.post(
                "/api/v1/files",
                files={"file": ("nota.txt", b"contenido")},
                data={"describe_images": "true"},
            ).json()
            state = wait_for_file(client, body["file_id"])
        assert state["describe_images"] is True

    def test_omitting_the_field_keeps_the_previous_behaviour(self, tmp_path) -> None:
        """Contrato retrocompatible: un cliente que no sepa del campo sigue
        recibiendo exactamente lo de antes."""
        with make_client(tmp_path, images=IngestionImagesConfig(enabled=True, model="visor")) as client:
            body = client.post(
                "/api/v1/files", files={"file": ("nota.txt", b"contenido")},
            ).json()
        assert body["describe_images"] is True


class TestDashboardUpload:
    def test_the_panel_offers_the_choice_and_honours_it(self, tmp_path) -> None:
        images = IngestionImagesConfig(enabled=True, model="visor")
        with make_client(tmp_path, images=images) as client:
            page = client.get("/dashboard/files")
            token = client.cookies.get("ai_broker_dashboard_csrf")
            posted = client.post(
                "/dashboard/actions/files/upload",
                headers={"X-CSRF-Token": token},
                files={"file": ("nota.txt", b"contenido del panel")},
                data={"csrf_token": token, "describe_images": "no"},
                follow_redirects=False,
            )
            listed = client.get(f"/api/v1/files/{_only_file_id(tmp_path)}").json()

        assert page.status_code == 200
        assert 'name="describe_images"' in page.text
        # El desplegable dice qué hará la opción heredada, que es la única que
        # el usuario no puede deducir mirando el formulario.
        assert "ahora: describir" in page.text
        assert posted.status_code == 303
        # La global dice "describir" y el formulario dijo "no": manda el formulario.
        assert listed["describe_images"] is False


def _only_file_id(tmp_path: Path) -> str:
    """No hay listado de ficheros en la API; el panel lo pinta desde la base."""
    rows = Database(tmp_path / "broker.db").query_all("SELECT id FROM ingested_files")
    assert len(rows) == 1
    return str(rows[0]["id"])
