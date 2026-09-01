# Resumen ejecutivo de actualización (conservador)

Objetivo: recuperar una línea de soporte coherente con **mínimo cambio**, sin re-arquitecturar. La prioridad es retirar Python 3.10 antes de su EOL de octubre de 2026, alinear tooling/CI y hacer reproducible la instalación.

Propuesta conservadora:
- Runtime mínimo: **Python 3.12**.
- CI: matriz **3.12 + 3.14** en Windows/Ubuntu.
- Ruff/Mypy: target **3.12**.
- Mantener FastAPI/Pydantic actuales salvo bumps patch/minor compatibles.
- Mantener SQLite, FastAPI, providers y arquitectura actuales.
- Mantener sandbox en Python 3.12 para evitar cambio innecesario.
- Introducir lockfile reproducible con hashes y un audit de dependencias.
- Separar configuración de ejemplo de configuración local sin tocar semántica.

# Fuentes de versiones (FUENTES_DE_VERSION)

| Componente | Versión detectada | Fichero fuente | Confianza | Comentario |
|---|---|---|---|---|
| Python app | `>=3.10` | `pyproject.toml` | Alta | rango de paquete |
| Python CI | `3.14` | `.github/workflows/ci.yml` | Alta | único runtime testeado |
| Python Ruff | `py310` | `pyproject.toml` | Alta | desalineado con CI |
| Python Mypy | `3.10` | `pyproject.toml` | Alta | desalineado con CI |
| Python sandbox | `3.12-slim` | `sandbox/Dockerfile` | Alta | runtime independiente |
| FastAPI | `0.139.0` | `requirements.lock` | Alta | PyPI muestra 0.141.1 más nueva al 2026-09-01 |
| Starlette | `1.3.1` | `requirements.lock` | Alta | transitiva; PyPI muestra 1.6.0 más nueva |
| Pydantic | `2.13.4` | `requirements.lock` | Alta | pin exacto |
| httpx | `0.28.1` | `requirements.lock` | Alta | pin exacto |
| Uvicorn | `0.51.0` | `requirements.lock` | Alta | pin exacto |
| Ruff | `0.15.21` | `requirements.lock` | Alta | pin exacto |
| Mypy | `2.2.0` | `requirements.lock` | Alta | pin exacto |
| Pytest | `9.1.1` | `requirements.lock` | Alta | pin exacto |
| checkout action | `v5` | `.github/workflows/ci.yml` | Alta | Node 24 |
| setup-python action | `v6` | `.github/workflows/ci.yml` | Alta | Node 24 |
| SQLite | stdlib Python | `app/db.py` | Media | versión depende de runtime |
| Docker sandbox base | `python:3.12-slim` | `sandbox/Dockerfile` | Alta | tag no fija digest |

# Referencias (EOL/soporte/CVEs/breaking changes) con fecha consultada

Fecha consultada: **2026-09-01**.

1. **Python 3.10**: Python.org indica estado “security” y fin de soporte **2026-10**. Fuente: https://www.python.org/getit/ y release pages de Python 3.10.
2. **Python 3.12**: soporte de seguridad hasta **2028-10** según PEP 693 / Python.org. Fuente: https://peps.python.org/pep-0693/
3. **Python 3.14**: rama en bugfix, soporte hasta aproximadamente **2030-10** según PEP 745. Fuente: https://peps.python.org/pep-0745/
4. **FastAPI 0.139.0**: publicado 2026-07-01, requiere Python >=3.10; PyPI marca 0.141.1 como release más nueva. Fuente: https://pypi.org/project/fastapi/0.139.0/
5. **Starlette 1.3.1**: publicado 2026-06-12; PyPI marca 1.6.0 como release más nueva. Fuente: https://pypi.org/project/starlette/1.3.1/
6. **actions/checkout@v5**: migró a Node 24 y requiere Actions Runner >=2.327.1. Fuente oficial: https://github.com/actions/checkout
7. **actions/setup-python@v6**: migró a Node 24 y requiere Actions Runner >=2.327.1. Fuente oficial: https://github.com/actions/setup-python
8. **CVEs**: esta auditoría no ejecutó un advisory scanner contra todo el lock. No se afirma ausencia de CVEs. Añadir `pip-audit`/OSV como gate es parte del plan.

# Matriz de obsolescencia (MATRIZ_DE_OBSOLESCENCIA)

| Área | Estado | Evidencia | Consecuencia | Acción |
|---|---|---|---|---|
| Python mínimo 3.10 | **Obsoleto inminente** | EOL 2026-10 | soporte/seguridad | subir mínimo a 3.12 |
| Python CI 3.14 | OK | rama bugfix hasta 2027, seguridad 2030 | bajo | mantener como runtime superior |
| Ruff/Mypy target 3.10 | Riesgo | desalineado con CI | análisis no representa runtime real | target 3.12 |
| FastAPI 0.139.0 | OK/Riesgo bajo | release julio 2026; 0.141.1 más nueva | fixes menores pendientes | evaluar bump compatible |
| Starlette 1.3.1 | Riesgo bajo | release junio 2026; 1.6.0 más nueva | transitiva; breaking potencial | no forzar; resolver vía FastAPI |
| Lockfile | Riesgo | exacto sin hashes/proceso declarado | supply-chain/reproducibilidad | generar con hashes |
| GitHub Actions | Riesgo | majors no actuales; requisito runner | CI self-hosted puede romper | documentar/pin SHA |
| Sandbox Python 3.12 | OK | soporte seguridad hasta 2028 | bajo | mantener en conservador |
| Config local versionada | Riesgo | IP/ruta/estado máquina | despliegue no portable | separar ejemplo/local |

# Targets recomendados (mínimo viable) y justificación

| Componente | Target conservador | Justificación |
|---|---|---|
| Python package | `>=3.12,<3.15` o `>=3.12` según política | elimina 3.10/3.11 sin forzar refactor a 3.14 |
| Ruff target | `py312` | coincide con mínimo |
| Mypy | `python_version=3.12` | coincide con mínimo |
| CI | Python 3.12 y 3.14 | prueba mínimo y runtime actual |
| Sandbox | `python:3.12-slim` | ya alineado con mínimo |
| FastAPI | 0.139.x → último compatible probado | cambio pequeño; no saltar transitivas a ciegas |
| Starlette | resuelta por FastAPI | evitar pin transitivo independiente |
| Actions | mantener majors si hosted; pin SHA | mínimo cambio y supply-chain más estable |
| Lock | exacto + hashes | reproducibilidad |

# Plan de cambios por área

## Runtime/lenguaje
1. Cambiar `requires-python` a `>=3.12`.
2. Ruff `target-version="py312"`.
3. Mypy `python_version="3.12"`.
4. Añadir CI 3.12 y conservar 3.14.
5. Revisar uso de módulos stdlib eliminados en 3.13; la lectura estática no detectó importaciones obvias de módulos PEP 594 críticos.

## Dependencias
1. Elegir herramienta de lock (pip-tools/uv).
2. Regenerar desde `pyproject.toml`.
3. Añadir hashes.
4. Bump de FastAPI solo tras resolver transitivas y ejecutar suite.
5. Añadir `pip-audit -r requirements.lock` o equivalente.

## Toolchain/build
1. Alinear Ruff/Mypy.
2. Añadir `python -m build`/instalación limpia como gate si se publica paquete.
3. Documentar comando exacto de regeneración de lock.

## Config
1. Crear `broker_config.example.yaml` portable.
2. Default `server.host: 127.0.0.1`.
3. Sustituir ruta ffmpeg por resolución PATH o variable local.
4. Mantener inventario/timestamps de modelos fuera del ejemplo versionado.

## CI/CD
1. Matriz runtime 3.12/3.14.
2. Pin actions por SHA.
3. Audit de dependencias.
4. Secret scan.
5. Validar instalación desde lock.

## Infra
1. Mantener `python:3.12-slim`.
2. Opcional: fijar digest de la imagen tras validar pipeline de actualización.
3. No cambiar SQLite/Docker en este plan.

# Touchpoints de cambio (TOUCHPOINTS_DE_CAMBIO)

| Ruta | Símbolo/área | Cambio esperado | Riesgo | Test |
|---|---|---|---|---|
| `pyproject.toml` | project/tool.ruff/tool.mypy | Python 3.12 | 12/Medium | install + Ruff + Mypy + pytest |
| `.github/workflows/ci.yml` | job matrix | 3.12/3.14 + audits | 12/Medium | CI ambos SO |
| `requirements.lock` | todo | regenerar/hash/bump menor | 12/Medium | clean install |
| `sandbox/Dockerfile` | FROM | mantener 3.12; opcional digest | 4/Low | sandbox smoke |
| `broker_config.yaml` | server/ingestion/providers | mover valores locales | 15/High | config load + startup |
| `.gitignore` | config/state | ignorar config local | 6/Low | repo status |
| `README.md` | instalación | runtime/lock/config | 6/Low | revisión |
| `Deployment_Guide.md` | prerequisites | Python soportado/runner | 6/Low | revisión |
| `app/config.py` | `load_config`, modelos | soportar ejemplo/local sin semántica nueva | 9/Medium | config tests |
| `app/startup.py` | guards | sin cambio funcional; revisar logs | 4/Low | startup tests |
| `tests/*` | suite | ajustar expectativas solo si rompe por 3.12/3.14 | 12/Medium | pytest |
| `.github/dependabot.yml` (nuevo, opcional) | updates | actions/pip | 4/Low | PR automation |

# Roadmap (Quick wins / Medio / Largo)

## Quick wins
- Python mínimo 3.12 + tooling.
- Matriz CI 3.12/3.14.
- Config example/local.
- Dependency audit.
- Pin SHAs de actions.

## Medio
- Regeneración de lock con hashes.
- Bumps patch/minor de FastAPI y dependencias directas.
- Validar Docker base digest y actualización periódica.

## Largo
- Nada arquitectónico obligatorio en el plan conservador; migraciones/refactors pertenecen al plan de modernización.

# TAREAS_UPGRADE_CONSERVADOR

## UGC-01 — Alinear runtime mínimo
- **Archivos:** `pyproject.toml`, `.github/workflows/ci.yml`
- **Pasos:** subir mínimo a 3.12; Ruff/Mypy 3.12; CI 3.12+3.14.
- **Aceptación:** instalación limpia y quality gates pasan en ambos runtimes.
- **Dependencias:** ninguna.
- **Prioridad:** P0.
- **Severidad/riesgo:** High, 15.
- **Esfuerzo:** S.

## UGC-02 — Separar configuración portable/local
- **Archivos:** `broker_config.yaml`, `.gitignore`, README/Deployment Guide.
- **Pasos:** crear ejemplo seguro; mover IP/rutas/model inventory a config local.
- **Aceptación:** clon limpio arranca con plantilla sin datos del equipo del autor.
- **Dependencias:** UGC-01 no obligatoria.
- **Prioridad:** P0.
- **Severidad/riesgo:** High, 15.
- **Esfuerzo:** M.

## UGC-03 — Formalizar lockfile
- **Archivos:** `requirements.lock`, docs, CI.
- **Pasos:** seleccionar generador; generar pins+hashes; verificar drift.
- **Aceptación:** instalación reproducible desde cero; CI detecta lock desactualizado.
- **Dependencias:** UGC-01.
- **Prioridad:** P1.
- **Severidad/riesgo:** Medium, 12.
- **Esfuerzo:** M.

## UGC-04 — Añadir security/dependency gates
- **Archivos:** CI.
- **Pasos:** dependency audit + secret scan.
- **Aceptación:** PR falla ante advisory severo o secreto confirmado.
- **Dependencias:** UGC-03.
- **Prioridad:** P0.
- **Severidad/riesgo:** Medium, 9.
- **Esfuerzo:** S.

## UGC-05 — Actualizaciones menores compatibles
- **Archivos:** lock.
- **Pasos:** resolver último FastAPI compatible; no forzar Starlette aislada; ejecutar suite.
- **Aceptación:** API/contratos y tests sin regresiones.
- **Dependencias:** UGC-03.
- **Prioridad:** P1.
- **Severidad/riesgo:** Low-Medium, 6.
- **Esfuerzo:** S/M.

# Verificación y checklist post-upgrade

- [ ] instalación limpia desde lock
- [ ] Ruff
- [ ] Mypy
- [ ] pytest
- [ ] coverage >= 90% según configuración
- [ ] smoke de `create_app`
- [ ] startup loopback
- [ ] startup LAN sin token falla cerrado
- [ ] subida/ingesta mínima
- [ ] provider mock/contract tests
- [ ] sandbox smoke sin red
- [ ] dependency audit
- [ ] secret scan
- [ ] config de ejemplo portable en Windows y Ubuntu

# Supuestos y límites

- No se ejecutaron los pasos anteriores durante la auditoría.
- No se recomienda un bump transitivo de Starlette aislado de FastAPI sin resolver compatibilidad.
- El target 3.12 es deliberadamente conservador: está en security-fixes-only, pero tiene soporte hasta 2028 y coincide con el sandbox actual.
