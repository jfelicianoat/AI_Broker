from pathlib import Path

import pytest

import app.dashboard_forms
import app.logging_config
import app.main

_REAL_LOGS = (Path(__file__).resolve().parents[1] / "logs").resolve()


@pytest.fixture(autouse=True, scope="session")
def keep_tests_out_of_the_real_log(tmp_path_factory):
    """Una config por defecto apunta a `logs/`, el fichero del broker real.

    Sin esto, cada pasada de tests dejaba allí eventos ficticios (modelos
    `large`, `small`...) mezclados con los de producción, y cualquier análisis
    del log —el modo sombra de System-1, por ejemplo— contaba ambos.
    """
    original = app.logging_config.configure_logging
    sandbox = tmp_path_factory.mktemp("logs")

    def guarded(config):
        if Path(config.directory).resolve() == _REAL_LOGS:
            config = config.model_copy(update={"directory": str(sandbox)})
        return original(config)

    patch = pytest.MonkeyPatch()
    for module in (app.logging_config, app.main, app.dashboard_forms):
        patch.setattr(module, "configure_logging", guarded)
    yield
    patch.undo()
