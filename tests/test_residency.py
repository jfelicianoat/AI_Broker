"""El contraste entre presupuesto y residencia real.

Lo que se protege aquí es la distinción que costó descubrir midiendo: una
reserva sin modelo cargado es NORMAL durante una carga en frío y solo es un
desalojo silencioso si coincide con un `evicting` reciente del runtime. Sin
esa correlación el panel gritaría en cada arranque en frío, que es justo el
falso positivo que haría que se dejara de mirar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import residency
from app.residency import GIB, build_report

RECENT_EVICTION = {
    "at": datetime.now(timezone.utc) - timedelta(minutes=2),
    "predicted_bytes": 20 * GIB,
    "gpu_free_bytes": 68 * GIB,
    "system_free_bytes": 14 * GIB,
}
OLD_EVICTION = {**RECENT_EVICTION, "at": datetime.now(timezone.utc) - timedelta(hours=20)}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """Ni log, ni LM Studio, ni memoria real: cada test declara su escenario."""
    monkeypatch.setattr(residency, "scan_runtime_log", lambda since_hours=48: {
        "evictions": [], "pool_bytes": None, "available": True,
    })
    monkeypatch.setattr(residency, "lmstudio_loaded_models", lambda urls, timeout=3.0: [])
    monkeypatch.setattr(residency, "system_free_bytes", lambda: 46 * GIB)


def _log(monkeypatch, evictions, pool_bytes=None):
    monkeypatch.setattr(residency, "scan_runtime_log", lambda since_hours=48: {
        "evictions": list(evictions), "pool_bytes": pool_bytes, "available": True,
    })


def _report(**kwargs):
    base = {
        "budget_bytes": 110 * GIB,
        "reserved_bytes": 0,
        "loaded_models": [],
        "lmstudio_urls": [],
    }
    return build_report(**{**base, **kwargs})


def levels(report) -> list[str]:
    return [finding.level for finding in report.findings]


def titles(report) -> str:
    return " | ".join(finding.title for finding in report.findings)


def test_carga_en_frio_no_se_reporta_como_desalojo():
    """Reserva puesta y modelo aún sin aparecer: es una carga, no una pérdida."""
    report = _report(reserved_bytes=16 * GIB, loaded_models=[])
    assert levels(report) == ["info"]
    assert "carga en frío" in report.findings[0].detail


def test_reserva_sin_modelo_con_desalojo_reciente_es_problema(monkeypatch):
    _log(monkeypatch, [RECENT_EVICTION])
    report = _report(reserved_bytes=16 * GIB, loaded_models=[])
    assert "problema" in levels(report)
    assert "Desalojo silencioso" in titles(report)


def test_desalojo_antiguo_no_explica_la_reserva_de_ahora(monkeypatch):
    """Fuera de la ventana de correlación no se le puede atribuir el hueco."""
    _log(monkeypatch, [OLD_EVICTION])
    report = _report(reserved_bytes=16 * GIB, loaded_models=[])
    assert "Desalojo silencioso" not in titles(report)
    assert "info" in levels(report)


def test_hueco_por_debajo_de_la_tolerancia_no_dice_nada():
    report = _report(reserved_bytes=int(0.5 * GIB), loaded_models=[])
    assert report.findings == []


def test_reserva_cubierta_por_un_modelo_con_lease_no_es_hueco():
    report = _report(
        reserved_bytes=16 * GIB,
        loaded_models=[{"model": "m", "size_vram_bytes": 16 * GIB, "lease_count": 1}],
    )
    assert "Reserva por delante" not in titles(report)


def test_modelo_sin_lease_no_cuenta_como_reserva_cubierta():
    """Un residente ocioso no justifica una reserva: no lo sostiene nadie."""
    report = _report(
        reserved_bytes=16 * GIB,
        loaded_models=[{"model": "m", "size_vram_bytes": 16 * GIB, "lease_count": 0}],
    )
    assert "Reserva por delante" in titles(report)


def test_desalojo_con_gpu_holgada_se_marca_como_problema(monkeypatch):
    _log(monkeypatch, [RECENT_EVICTION, OLD_EVICTION])
    report = _report()
    assert "desalojo(s) con la GPU holgada" in titles(report)
    assert "problema" in levels(report)


def test_desalojo_con_gpu_ajustada_no_es_el_fallo_de_pool_equivocado(monkeypatch):
    """Si la GPU tampoco tenía sitio, el desalojo fue legítimo."""
    _log(monkeypatch, [{**RECENT_EVICTION, "gpu_free_bytes": 21 * GIB}])
    report = _report()
    assert "GPU holgada" not in titles(report)


def test_lmstudio_cargado_se_declara_fuera_del_presupuesto(monkeypatch):
    monkeypatch.setattr(residency, "lmstudio_loaded_models", lambda urls, timeout=3.0: ["a", "b"])
    report = _report()
    assert "no cuenta nadie" in titles(report)
    assert report.lmstudio_loaded == ["a", "b"]


def test_modelo_mayor_que_la_ram_libre_ya_no_pasaria_admision(monkeypatch):
    monkeypatch.setattr(residency, "system_free_bytes", lambda: 10 * GIB)
    report = _report(loaded_models=[{"model": "m", "size_vram_bytes": 30 * GIB, "lease_count": 0}])
    assert "ya no pasaría la admisión" in titles(report)


def test_margen_estrecho_avisa_antes_de_romper(monkeypatch):
    monkeypatch.setattr(residency, "system_free_bytes", lambda: 40 * GIB)
    report = _report(loaded_models=[{"model": "m", "size_vram_bytes": 30 * GIB, "lease_count": 0}])
    assert "Poco margen" in titles(report)


def test_presupuesto_por_encima_del_pool_real(monkeypatch):
    _log(monkeypatch, [], pool_bytes=99 * GIB)
    report = _report(budget_bytes=112 * GIB)
    assert "excede el pool real" in titles(report)


def test_presupuesto_dentro_del_pool_no_avisa(monkeypatch):
    _log(monkeypatch, [], pool_bytes=99 * GIB)
    report = _report(budget_bytes=96 * GIB)
    assert "excede el pool real" not in titles(report)


def test_sin_memoria_legible_no_se_inventa_un_techo(monkeypatch):
    """En una máquina donde no se puede leer la RAM el panel calla, no adivina."""
    monkeypatch.setattr(residency, "system_free_bytes", lambda: None)
    report = _report(loaded_models=[{"model": "m", "size_vram_bytes": 90 * GIB, "lease_count": 0}])
    assert report.system_free_bytes is None
    assert "admisión" not in titles(report)


def test_estado_limpio_no_produce_hallazgos():
    report = _report(loaded_models=[{"model": "m", "size_vram_bytes": 16 * GIB, "lease_count": 0}])
    assert report.findings == []


def test_roots_locales_pierden_el_sufijo_v1():
    """El estado de carga no vive bajo el prefijo compatible con OpenAI."""

    class Provider:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Config:
        class providers:
            custom = [
                Provider(enabled=True, deployment="local", base_url="http://127.0.0.1:1234/v1"),
                Provider(enabled=True, deployment="cloud", base_url="https://api.remoto/v1"),
                Provider(enabled=False, deployment="local", base_url="http://127.0.0.1:9999/v1"),
            ]

    assert residency.local_openai_provider_roots(Config) == ["http://127.0.0.1:1234"]
