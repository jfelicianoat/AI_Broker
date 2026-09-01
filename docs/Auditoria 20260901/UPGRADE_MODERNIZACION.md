# Resumen ejecutivo de modernización

La modernización propuesta usa **Python 3.14** como runtime principal y conserva la semántica del producto, pero reduce deuda estructural. El foco no es “reescribir”: es convertir mega-módulos en boundaries comprobables, hacer reproducible la supply chain, separar configuración/estado y elevar observabilidad.

Prioridades:
1. Fundación reproducible y segura.
2. Runtime/tooling 3.14 coherente.
3. Extraer estrategias, repositorios, schemas y dashboard por dominios.
4. Migraciones versionadas.
5. Observabilidad con métricas/trazas y contract tests de providers.
6. Hardening de MCP/sandbox/config.

# Diferencias clave vs plan conservador

| Tema | Conservador | Modernización |
|---|---|---|
| Python | mínimo 3.12, CI 3.12+3.14 | 3.14 como baseline |
| Arquitectura | no tocar salvo necesidad | descomponer mega-módulos |
| DB | SQLite actual | SQLite se mantiene, migraciones versionadas y repos por agregado |
| Observabilidad | logs actuales mejorados | métricas + trazas + decision telemetry |
| Tests | suite actual + audits | contract tests, state-machine tests, arquitectura |
| Config | example/local | separar defaults, secrets, discovery state y operator overrides |
| CI | gates básicos | quality gates + SBOM/audit + pin supply-chain |
| Providers | mantener adapters | boundaries/probing/transport separados |
| Dashboard | mantener | routers/view-models/form DTOs tipados |

# Targets recomendados (modernización) y justificación

| Componente | Target | Justificación |
|---|---|---|
| Python | 3.14.x | rama bugfix activa; soporte hasta ~2030 |
| Ruff/Mypy | target 3.14 | elimina desalineación |
| FastAPI | última release compatible validada (0.141.x al consultar) | fixes recientes sin esperar obsolescencia |
| Pydantic | última 2.x compatible | mantener major estable |
| Starlette | resuelta por FastAPI | evitar gestión transitiva manual |
| GitHub Actions | majors actuales + SHA | menor exposición supply-chain |
| Sandbox | 3.14-slim si libs de datos lo soportan; si no 3.12 como runtime aislado documentado | coherencia vs compatibilidad |
| Lock | lock nativo con hashes/metadata | reproducibilidad |
| Observabilidad | OpenTelemetry opcional + métricas Prometheus/OpenMetrics | trazabilidad operacional |

# Plan por áreas

## Arquitectura
- `coordinator.py` → package `app/strategies/`: single, map_reduce, agent, consensus, escalation.
- `providers/routing.py` → catálogo, selección, scoring, policy, execution.
- `repository.py` → repositories por agregado.
- `schemas.py` → dominio/API por bounded context.
- `dashboard_web.py` → routers por página y servicios de aplicación.
- `dashboard_forms.py` → DTOs tipados por formulario, eliminar override Mypy.
- `ingestion/service.py` → upload, conversion orchestration, enrichment, storage.

## Observabilidad
- Correlation IDs obligatorios: task/run/invocation/file.
- Métricas: queue wait, task duration, provider latency/error, retry, model load, VRAM waits, DB lock time, ingestion throughput, sandbox failures.
- Traces para una tarea completa.
- Eventos de routing con scoring y evidencia.
- Capturas amplias de excepción deben producir código de error + metric, salvo cancelación deliberada.

## Tests
- Contract tests por provider.
- State-transition tests de tareas/ingesta.
- Property tests de routing/constraints.
- Tests de auth/CSRF por superficie.
- Tests de migración desde snapshots antiguos.
- Test de arquitectura/import boundaries.

## CI
- Python 3.14 Windows/Ubuntu.
- Ruff/Mypy/pytest/cov.
- dependency audit + secret scan.
- build/package verification.
- SBOM.
- actions por SHA.
- cache reproducible.

## Dependencias
- Dependabot/Renovate.
- política mensual de actualización.
- lock generado y revisado.
- transitivas solo se controlan mediante constraints/solver, no pins ad hoc.

## Infra
- Docker image pinned por digest y renovada automáticamente.
- Rootless Docker si es compatible con el entorno.
- No montar Docker socket.
- SQLite WAL se mantiene mientras las métricas no demuestren contención.
- Config local fuera de git.

# Touchpoints

## PHASE_0 — fundación y mayor riesgo

| Ruta/módulo | Símbolos/área | Cambio | Riesgo | Validación |
|---|---|---|---|---|
| `pyproject.toml` | project/Ruff/Mypy | Python 3.14 | 16/High | CI |
| `requirements.lock` | deps | nuevo locking | 12/Medium | clean install/audit |
| `.github/workflows/ci.yml` | workflow | gates modernos | 12/Medium | CI |
| `broker_config.yaml` | todo | convertir en ejemplo/default | 15/High | config tests |
| `app/main.py` | `create_app`, routers | extraer routers | 12/Medium | API tests |
| `app/startup.py` | guards/processes | lifecycle observable | 9/Medium | startup tests |
| `app/admin_auth.py` | auth/throttle | conservar contrato | 10/Medium | security tests |
| `app/db.py` | schema/init/transaction | migraciones versionadas | 12/Medium | migration tests |
| `app/repository.py` | TaskRepository | preparar split | 16/High | repository tests |
| `app/coordinator.py` | `ConsensusCoordinator` | façade + strategies | 20/High | strategy tests |
| `app/providers/base.py` | contracts/errors | interfaces estables | 12/Medium | contract tests |
| `app/providers/routing.py` | routing | separar policy/scoring | 16/High | routing tests |
| `app/sandbox.py` | `SandboxExecutor` | hardening/lifecycle | 10/Medium | sandbox tests |
| `app/mcp.py` | registry/spawn | env mínimo + stderr seguro | 10/Medium | MCP fixtures |

## PLAN_POR_FASES

### Fase 0 — Fundación
- Runtime 3.14 y lock reproducible.
- CI completo.
- Config portable/local.
- Métricas base y correlation IDs.
- Migrator versionado sin cambiar schema lógico.
- Caracterización de APIs/contratos actuales.

### Fase 1 — Upgrades mayores
- FastAPI/Pydantic/direct deps a últimas compatibles.
- Actions actuales.
- Sandbox 3.14 si el ecosistema científico/Docling lo valida.
- Corregir incompatibilidades detectadas por tests.

### Fase 2 — Refactors y deuda técnica
**Lote A: core execution**
- `app/coordinator.py`
- `app/strategy_router.py`
- `app/task_classifier.py`
- `app/resource_scheduler.py`

**Lote B: providers**
- `app/providers/base.py`
- `routing.py`
- `ollama.py`
- `openai_compatible.py`
- `deepseek.py`
- bootstrap/diagnostics

**Lote C: persistence/domain**
- `app/repository.py`
- `app/db.py`
- `app/schemas.py`
- model stats/fingerprints/quarantine/references/timing

**Lote D: ingestion**
- `app/ingestion/*`
- `app/artifacts.py`
- `app/evidence.py`

**Lote E: dashboard**
- `app/dashboard_web.py`
- `app/dashboard_forms.py`
- `app/dashboard.py`
- filters/templates/static

**Lote F: tools/ops**
- `app/skills.py`
- `app/mcp.py`
- `app/sandbox.py`
- `app/maintenance.py`
- `app/health.py`
- `app/startup.py`
- scripts

### Fase 3 — Hardening
- threat model formal de MCP/sandbox/LAN.
- chaos/failure injection de providers.
- métricas SLO.
- load tests de cola/SQLite.
- restore/backup drill.
- SBOM y política de advisories.

## NEXT_PHASE_ASK
Para profundizar con touchpoints 100% símbolo-a-símbolo, el siguiente lote debe elegirse por módulo. Recomendación: **Lote A — core execution (`app/coordinator.py`, `strategy_router.py`, `task_classifier.py`, `resource_scheduler.py`)** porque concentra el mayor riesgo arquitectónico.

# Roadmap (fases claras)

## Fase 0 — 1 a 2 iteraciones
Fundación reproducible, 3.14, CI, config, migrator y observabilidad base.

## Fase 1 — 1 iteración
Upgrades directos y compatibilidad.

## Fase 2 — varias iteraciones por bounded context
Extraer módulos sin cambiar contratos externos; cada extracción debe quedar cubierta por tests de caracterización.

## Fase 3 — hardening
Seguridad, performance, resiliencia y operación.

# TAREAS_UPGRADE_MODERNIZACION

## UGM-01 — Baseline Python 3.14
- **Archivos:** `pyproject.toml`, CI, docs, opcional Dockerfile.
- **Pasos:** baseline 3.14; tooling alineado; validar dependencias nativas.
- **Aceptación:** suite completa en Windows/Ubuntu 3.14.
- **Dependencias:** ninguna.
- **Prioridad:** P0.
- **Severidad/riesgo:** High/16.
- **Esfuerzo:** M.

## UGM-02 — Supply chain reproducible
- **Archivos:** lock, CI, Dependabot/Renovate.
- **Pasos:** lock con hashes, SBOM, audit, pins SHA.
- **Aceptación:** build reproducible y audit automático.
- **Dependencias:** UGM-01.
- **Prioridad:** P0.
- **Severidad/riesgo:** High/15.
- **Esfuerzo:** M.

## UGM-03 — Configuración por capas
- **Archivos:** config YAML, `app/config.py`, docs.
- **Pasos:** defaults → operator overrides → secrets env/keyring → discovery state DB.
- **Aceptación:** repo no contiene IP/rutas/inventario del autor como defaults.
- **Dependencias:** ninguna.
- **Prioridad:** P0.
- **Severidad/riesgo:** High/15.
- **Esfuerzo:** M.

## UGM-04 — Extraer estrategias del coordinador
- **Archivos:** `app/coordinator.py` + `app/strategies/*`.
- **Pasos:** caracterización; interfaces; mover single/map-reduce/agent/consensus; façade final.
- **Aceptación:** mismos contratos/eventos/resultados; `coordinator.py` deja de ser mega-módulo.
- **Dependencias:** UGM-01, tests verdes.
- **Prioridad:** P1.
- **Severidad/riesgo:** Critical-ish architecture, 20.
- **Esfuerzo:** L.

## UGM-05 — Modularizar routing/providers
- **Archivos:** `app/providers/*`.
- **Pasos:** separar transport, catalog, probes, scoring, policy.
- **Aceptación:** adapters cumplen contract suite común.
- **Dependencias:** UGM-04 parcialmente.
- **Prioridad:** P1.
- **Severidad/riesgo:** High/16.
- **Esfuerzo:** L.

## UGM-06 — Repositories y migraciones
- **Archivos:** `repository.py`, `db.py`, nuevos repositories/migrations.
- **Pasos:** versionar schema; separar agregados; snapshots de migración.
- **Aceptación:** migración desde DB histórica y rollback/backup documentado.
- **Dependencias:** UGM-02.
- **Prioridad:** P1.
- **Severidad/riesgo:** High/16.
- **Esfuerzo:** L.

## UGM-07 — Schemas por bounded context
- **Archivos:** `schemas.py` + nuevos módulos.
- **Pasos:** separar task/file/model/dashboard/provider contracts.
- **Aceptación:** OpenAPI/API pública sin cambios no intencionados.
- **Dependencias:** UGM-04/06.
- **Prioridad:** P1.
- **Severidad/riesgo:** High/16.
- **Esfuerzo:** L.

## UGM-08 — Dashboard modular y tipado
- **Archivos:** `dashboard_web.py`, `dashboard_forms.py`, templates.
- **Pasos:** routers por página; DTOs Pydantic; retirar override Mypy.
- **Aceptación:** Mypy sin suppressions de módulo; CSRF/auth regresión verde.
- **Dependencias:** UGM-07.
- **Prioridad:** P1.
- **Severidad/riesgo:** High/16.
- **Esfuerzo:** L.

## UGM-09 — Ingestion services
- **Archivos:** `app/ingestion/*`.
- **Pasos:** separar upload/conversion/enrichment/storage; reutilizar clientes HTTP.
- **Aceptación:** mismos formatos, dedupe, timeouts y estados.
- **Dependencias:** UGM-07.
- **Prioridad:** P2.
- **Severidad/riesgo:** High/16.
- **Esfuerzo:** L.

## UGM-10 — Observabilidad estándar
- **Archivos:** logging/health/coordinator/providers/ingestion.
- **Pasos:** correlation IDs, métricas, trazas opcionales, dashboards/SLO.
- **Aceptación:** una tarea puede seguirse extremo a extremo.
- **Dependencias:** Fase 0 parcial.
- **Prioridad:** P1.
- **Severidad/riesgo:** Medium/12.
- **Esfuerzo:** M/L.

## UGM-11 — Hardening MCP/sandbox
- **Archivos:** `mcp.py`, `sandbox.py`, config/docs.
- **Pasos:** entorno mínimo, allowlist comando, stderr acotado, rootless si viable, lifecycle limpio.
- **Aceptación:** threat model y tests negativos.
- **Dependencias:** UGM-03.
- **Prioridad:** P2.
- **Severidad/riesgo:** Medium-High/10.
- **Esfuerzo:** M.

# Verificación y checklist post-modernización

- [ ] build/install reproducible desde cero
- [ ] Python 3.14 Windows + Ubuntu
- [ ] Ruff/Mypy sin override global de `dashboard_forms`
- [ ] pytest + cobertura >= 90%
- [ ] contract tests providers
- [ ] migration tests desde DB antigua
- [ ] auth/CSRF negative tests
- [ ] LAN fail-closed test
- [ ] MCP boundary tests
- [ ] sandbox no-network/read-only/resource-limit tests
- [ ] ingestion corpus tests
- [ ] dependency audit + secret scan + SBOM
- [ ] traces/métricas con task_id/run_id/invocation_id
- [ ] carga básica de cola y SQLite
- [ ] backup/restore probado

# Supuestos y límites

- No se ejecutó el proyecto ni la suite.
- No se propone migrar fuera de SQLite sin evidencia de contención real.
- No se propone reescribir en otro framework/lenguaje.
- La modernización debe preservar contratos externos y eventos salvo ADR/migración explícita.
- El touchpoint exhaustivo a nivel de los ~2.3k símbolos Python del repo no cabe de forma útil en un único documento; se entrega PHASE_0 + plan por módulos conforme a la KB.
