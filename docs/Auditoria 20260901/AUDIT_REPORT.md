# Resumen ejecutivo

- **Modo proyecto.** Auditoría por lectura estática del ZIP `AI_Broker-main.zip`; no se ha ejecutado el broker, la suite, Docker, Ollama, LM Studio ni herramientas externas.
- El diseño general es sólido para un broker local/multi-LLM: FastAPI como façade HTTP, SQLite como estado durable, coordinador de estrategias, capa de providers, ingesta asíncrona, scheduler de recursos, sandbox Docker y dashboard operativo.
- **Fortaleza de seguridad:** el arranque falla cerrado cuando el servidor escucha fuera de loopback sin credencial (`app/startup.py:57-81`), salvo opt-out explícito `allow_unauthenticated_lan=true`.
- **Fortaleza de aislamiento:** `app/sandbox.py` construye procesos con `create_subprocess_exec` (sin shell) y Docker con `--network none`, límites de memoria/CPU/PIDs y filesystem read-only.
- **High / Maintainability-Architecture (riesgo 16):** varios módulos concentran demasiadas responsabilidades: `app/coordinator.py` ~2965 líneas, `app/dashboard_web.py` ~1762, `app/repository.py` ~1558, `app/dashboard_forms.py` ~1319, `app/schemas.py` ~1318 y `app/providers/routing.py` ~1273. Esto aumenta coste de cambio y riesgo de regresión.
- **High / Config (riesgo 15):** `broker_config.yaml` está versionando configuración altamente específica del equipo del autor (IP LAN `192.168.1.52`, ruta absoluta Windows a ffmpeg, inventario/modelos y timestamps de compatibilidad). No se observó un secreto literal, pero sí fuerte acoplamiento a entorno y datos operativos.
- **High / Obsolescence (riesgo 15):** `pyproject.toml` admite Python `>=3.10`, mientras CI usa 3.14 y Ruff/Mypy targetean 3.10. Python 3.10 entra en EOL en octubre de 2026; a fecha 2026-09-01 queda aproximadamente un mes de soporte de seguridad.
- **Medium / Dependency-Reproducibility (riesgo 12):** `requirements.lock` fija dependencias, pero el `pyproject.toml` usa mínimos abiertos y el lock no contiene hashes. El proyecto depende de dos fuentes de verdad con semánticas distintas y no hay evidencia de proceso automatizado que regenere/verifique el lock.
- **Medium / CI (riesgo 9):** `actions/checkout@v5` y `actions/setup-python@v6` usan Node 24 y requieren runner >=2.327.1. En hosted runners es razonable; en self-hosted puede romper sin control de versión. Además, las acciones ya tienen majors más nuevas a fecha de consulta.
- **Medium / Reliability (riesgo 9):** se detectaron 65 capturas `except Exception`/`BaseException` en `app/`; varias son defensivas/best-effort y comentadas, pero el volumen dificulta distinguir fallos esperados de defectos silenciados.
- No se ha confirmado ninguna CVE explotable en las versiones bloqueadas mediante esta auditoría. No debe interpretarse como “sin vulnerabilidades”: falta ejecutar un escáner de advisories (`pip-audit`/OSV) contra el lock.

# Mapa del sistema

## Árbol resumido

```text
AI_Broker-main/
├─ app/                     # núcleo de aplicación (51 módulos Python, ~23.9k LOC)
│  ├─ main.py               # composition root + API HTTP + lifespan
│  ├─ coordinator.py        # estrategias single/agent/mixture/auto
│  ├─ repository.py / db.py # persistencia SQLite y repositorio
│  ├─ providers/            # Ollama, DeepSeek, OpenAI-compatible, routing
│  ├─ ingestion/            # detección, conversión, OCR/visión, worker
│  ├─ dashboard*.py         # consultas, formularios y router web
│  ├─ sandbox.py            # ejecución aislada de código generado
│  ├─ mcp.py                # cliente MCP stdio
│  └─ config.py/schemas.py  # configuración y contratos Pydantic
├─ tests/                   # suite amplia (39 ficheros de test aprox.)
├─ scripts/                 # arranque, backup, readiness, migración/operación
├─ .github/workflows/ci.yml # lint + mypy + pytest/cobertura en Windows/Ubuntu
├─ sandbox/Dockerfile       # Python 3.12 slim para ejecución aislada
├─ pyproject.toml           # metadata, deps mínimas, Ruff/Mypy/Pytest
├─ requirements.lock        # versiones exactas instaladas por CI
├─ broker_config.yaml       # configuración operativa concreta
└─ docs/, *.md              # arquitectura/producto/operación
```

## Entrypoints y flujo principal

- `scripts/run_broker.py` → arranque productivo y propagación de `--config`.
- `app.main:create_app` → composition root; carga config, logging, DB, repository, scheduler, providers, ingestion, sandbox, MCP y coordinator.
- API `/api/v1/tasks` → valida/crea tarea → repositorio → dispatcher → `ConsensusCoordinator`.
- `ConsensusCoordinator` selecciona estrategia (`single`, `mixture_of_agents`, `agent`, `auto`) y coordina providers, herramientas, límites y persistencia.
- `app/providers/routing.py` y adapters concretos resuelven modelos/proveedores.
- Adjuntos: `/api/v1/files` → `IngestionService` → worker/conversión → Markdown/imagen disponible para la tarea.
- Dashboard: router protegido + CSRF → consultas de `DashboardQueryRepository` y mutaciones sobre API/repositorio.
- Persistencia: SQLite en WAL, repositorio explícito y transacciones con `BEGIN IMMEDIATE`.

# Cómo funciona

El sistema actúa como gateway local de inferencia multi-modelo. La petición entra por FastAPI, se materializa como tarea durable en SQLite y se procesa de forma desacoplada por dispatchers. El coordinador decide una estrategia de ejecución y delega en una abstracción de proveedor que puede resolver Ollama, endpoints OpenAI-compatible o DeepSeek. El scheduler arbitra recursos locales; el repositorio guarda estado, invocaciones, eventos y artefactos.

Para agentes, el coordinador integra skills internas, MCP y herramientas del cliente. Para documentos, la ingesta separa detección, conversión y enriquecimiento visual/transcripción. Para código generado, `SandboxExecutor` usa Docker con fronteras explícitas. El dashboard es una superficie operativa separada, aunque actualmente sus routers/formularios siguen siendo módulos muy grandes.

# Hallazgos por fichero

## `pyproject.toml`
### Rol del fichero
Metadata del paquete, rango de Python, dependencias directas y configuración de Ruff/Mypy/Pytest/Coverage.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Obsolescence | 5 | 3 | 15 | `requires-python=">=3.10"`; Ruff `py310`; Mypy `3.10` | Retirar 3.10 antes de EOL | Conservador: `>=3.12`; modernización: `>=3.14` |
| Medium | Dependency | 3 | 4 | 12 | deps con mínimos `>=`, lock separado | Definir política única de locking | Generación reproducible y verificación CI |
| Medium | Testing | 3 | 3 | 9 | `mypy` tiene override amplio para `app.dashboard_forms` | Reducir deuda de typing del formulario | Dividir módulo y eliminar suppressions gradualmente |

## `requirements.lock`
### Rol del fichero
Conjunto exacto de versiones que instala CI.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Dependency | 3 | 4 | 12 | pins exactos pero sin hashes/metadatos de generación | Hacer lock verificable y regenerable | `pip-tools --generate-hashes`, uv lock o equivalente |
| Low | Obsolescence | 2 | 2 | 4 | FastAPI 0.139.0; existe 0.141.1 | Actualización menor tras test | bump conservador si compatibilidad confirmada |
| Low | Obsolescence | 2 | 2 | 4 | Starlette 1.3.1; existe 1.6.0, pero es dependencia transitiva | No pin manual aislado | actualizar a través de constraints compatibles con FastAPI |

## `.github/workflows/ci.yml`
### Rol del fichero
Quality gate de push/PR en Windows y Ubuntu.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Config/Compatibility | 4 | 4 | 16 | CI Python 3.14 vs metadata/tooling 3.10 | Matriz coherente con versiones soportadas | 3.12 + 3.14 conservador; 3.14 único/primario modernización |
| Medium | CI | 3 | 3 | 9 | checkout v5/setup-python v6 requieren runner >=2.327.1 | Documentar/actualizar runner | añadir requisito o mover a majors actuales |
| Medium | Security | 3 | 3 | 9 | acciones referenciadas por tag mutable | Pin SHA para supply-chain sensible | Dependabot/Renovate para actualizar SHAs |
| Medium | Security | 3 | 3 | 9 | sin paso explícito de dependency/advisory scan | Añadir `pip-audit`/OSV | quality gate de vulnerabilidades |

## `broker_config.yaml`
### Rol del fichero
Configuración operativa completa del broker.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Config | 5 | 3 | 15 | host LAN `192.168.1.52`; ffmpeg absoluto `C:\Users\...`; modelos/timestamps locales | Separar defaults versionables de estado local | `broker_config.example.yaml` + config local ignorada |
| Medium | Maintainability | 3 | 4 | 12 | ~94 KB de configuración con inventario de modelos | Externalizar catálogo/estado descubierto | DB/state generado en runtime |
| Medium | Security | 4 | 2 | 8 | el host por defecto está expuesto a LAN, aunque fail-closed exige token | Default seguro loopback | ejemplo en `127.0.0.1`, override local para LAN |

## `app/main.py`
### Rol del fichero
Composition root, lifespan, middleware y endpoints API.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Architecture | 4 | 3 | 12 | ~951 líneas; wiring + middleware + endpoints | Mantener composition root pequeño | routers por dominio: tasks/files/models/ops |
| Low | Security | 2 | 2 | 4 | `verify_admin_access` aplicado a operaciones sensibles; health/model info requiere revisión de exposición intencional | Documentar superficie pública | tests de autorización por ruta |
| Low | Reliability | 2 | 2 | 4 | lifespan concentra procesos auxiliares y mantenimiento | contratos de shutdown explícitos | pruebas de cierre/cancelación |

## `app/startup.py`
### Rol del fichero
Guards de arranque, detección VRAM y auto-start de proveedores locales.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Positive | Security | 5 | 1 | — | `ensure_admin_credential_for_exposed_host` fail-closed | Conservar | cubrir con test de regresión |
| Low | Observability | 2 | 3 | 6 | `detect_total_vram_gb` captura `Exception` y silencia | log debug estructurado | distinguir ejecutable ausente/error parseo |
| Low | Reliability | 2 | 2 | 4 | timeout de proceso devuelve 124 pero no se observa `kill()/wait()` en `run_process` | finalizar proceso al timeout | kill + await wait |

## `app/admin_auth.py`
### Rol del fichero
Resolución/verificación de token administrativo, cookie y throttling de login.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Positive | Security | 5 | 1 | — | verificación centralizada + keyring/env | Conservar | pruebas de timing/rotación |
| Medium | Security | 4 | 2 | 8 | modo `allow_unauthenticated_lan` desactiva guard por diseño | señalización operativa muy visible | banner/health degraded + docs explícitas |

## `app/dashboard_web.py`
### Rol del fichero
Router HTML, autenticación de dashboard, CSRF y handlers web.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Architecture | 4 | 4 | 16 | ~1762 líneas, ~84 funciones | Separar routers por página/caso de uso | `dashboard/routes/*.py` |
| Positive | Security | 4 | 1 | — | cookie CSRF + validación token; router protegido | Conservar | test negativo por cada mutación |
| Medium | Testing/Maintainability | 3 | 3 | 9 | lógica web y composición de vistas juntas | extraer servicios/view-models | handlers delgados |

## `app/dashboard_forms.py`
### Rol del fichero
Parseo/normalización de formularios del dashboard.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Maintainability | 4 | 4 | 16 | ~1319 líneas + override Mypy con múltiples códigos desactivados | reemplazar dicts ambiguos por modelos tipados | DTOs Pydantic por formulario |
| Medium | Correctness | 4 | 3 | 12 | typing debilitado precisamente en frontera de input humano | reforzar validación tipada | eliminar suppressions por lotes |

## `app/coordinator.py`
### Rol del fichero
Orquestador central de estrategias, agentes, consenso, escalado y artefactos.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Architecture | 5 | 4 | 20 | ~2965 líneas, 75 funciones, múltiples estrategias/ciclos | Descomponer por estrategia/capacidad | `strategies/single.py`, `agent.py`, `consensus.py`, `map_reduce.py` |
| High | Reliability | 4 | 4 | 16 | gran cantidad de catches defensivos/best-effort | taxonomía explícita de errores | catches estrechos + métricas |
| Medium | Testing | 4 | 3 | 12 | alta complejidad de estados y ramas | tests de máquina de estados/contratos | property/state-transition tests |

## `app/providers/routing.py`
### Rol del fichero
Selección/routing entre providers/modelos y políticas adaptativas.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Architecture | 4 | 4 | 16 | ~1273 líneas, 52 funciones | separar catálogo, scoring, policy y execution | interfaces internas y objetos de decisión |
| Medium | Observability | 3 | 3 | 9 | routing es crítico para explicar decisiones | estandarizar decision trace | evento estructurado con inputs/scoring |

## `app/providers/base.py`
### Rol del fichero
Contratos y utilidades comunes de providers.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Architecture | 3 | 3 | 9 | ~875 líneas, 7 clases, 39 funciones | separar tipos/errores/context sizing | módulos cohesionados |
| Positive | Reliability | 4 | 2 | — | normalización de errores/contexto centralizada | Conservar | contract tests entre adapters |

## `app/providers/ollama.py`
### Rol del fichero
Adapter Ollama, catálogo, lifecycle, memoria/offload y chat.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Maintainability | 4 | 4 | 16 | ~1021 líneas mezclando adapter + lifecycle + planificación GPU | separar lifecycle/offload/transport | 3 módulos internos |
| Positive | Reliability | 4 | 2 | — | timeouts, cachés, manejo explícito de respuestas vacías | Conservar | tests contractuales con payloads reales |

## `app/providers/openai_compatible.py`
### Rol del fichero
Adapter genérico OpenAI-compatible, probes y normalización de respuestas.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Maintainability | 3 | 3 | 9 | ~709 líneas, probes + ejecución + catálogo | separar probing de serving path | `probe.py` compartido |
| Positive | Reliability | 4 | 2 | — | errores HTTP y reasoning-only tratados explícitamente | Conservar | fixtures de proveedores divergentes |

## `app/repository.py`
### Rol del fichero
Repositorio de tareas, eventos, invocaciones, artefactos y estado.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Architecture | 4 | 4 | 16 | ~1558 líneas, 57 funciones | separar repositories por agregado | Task/Event/Invocation/File repositories |
| Low | Security | 2 | 1 | 2 | SQL dinámico en `update_task`, pero nombres de columnas provienen de lista interna y valores usan `?` | mantener whitelist interna | comentario/test para evitar futuros campos externos |
| Medium | Reliability | 3 | 3 | 9 | SQLite y muchos estados concurrentes | formalizar invariantes/transiciones | constraints + tests de carrera |

## `app/db.py`
### Rol del fichero
Conexión SQLite, schema/migraciones y helpers transaccionales.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Maintainability | 3 | 4 | 12 | migraciones embebidas en código de inicialización | versionar migraciones | Alembic o migrator propio versionado |
| Low | Security | 2 | 1 | 2 | `PRAGMA journal_mode` interpolado; valor proviene de config validada | whitelist estricta | conservar validación y test |
| Positive | Reliability | 4 | 1 | — | `RLock`, guard contra commit dentro de transacción, rollback en BaseException | Conservar | pruebas concurrentes |

## `app/ingestion/service.py`
### Rol del fichero
Orquestación de subida, dedupe, conversión, enriquecimiento y persistencia de ficheros.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Architecture | 4 | 4 | 16 | ~1228 líneas, 51 funciones | separar upload/conversion/enrichment/storage | servicios cohesionados |
| Positive | Security | 4 | 2 | — | streaming a temporal y controles de formato/tamaño descritos/implementados en módulos de ingesta | Conservar | fuzz tests de nombres/magic bytes |

## `app/ingestion/engines.py`
### Rol del fichero
Bindings a motores de conversión/visión/transcripción.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Low | Reliability | 2 | 2 | 4 | HTTP síncrono (`httpx.post`) con timeout explícito | asegurar que siempre corre fuera del event loop | documentar/encapsular cliente |
| Medium | Performance | 3 | 3 | 9 | clientes HTTP nuevos por llamada en path de imágenes | reutilizar cliente o pool | client lifecycle de ingesta |

## `app/sandbox.py`
### Rol del fichero
Ejecución aislada de Python generado por modelos.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Positive | Security | 5 | 1 | — | `create_subprocess_exec`, `--network none`, read-only, resource limits, sin shell | Conservar | tests que inspeccionen comando final |
| Medium | Security | 5 | 2 | 10 | frontera depende del daemon Docker del host; Docker comprometido anula aislamiento | documentar threat model | no montar socket; rootless si viable |
| Low | Reliability | 2 | 2 | 4 | timeout mata proceso attach, limpieza en `finally` | añadir `await process.wait()` tras kill | evitar zombies |

## `app/mcp.py`
### Rol del fichero
Cliente MCP stdio y lifecycle de servidores configurados.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Security | 5 | 2 | 10 | ejecuta comandos MCP configurados con entorno heredado | tratar config MCP como código privilegiado | allowlist/path explícito, env mínimo |
| Medium | Observability | 3 | 3 | 9 | stderr del servidor se descarta (`DEVNULL`) | capturar acotado/rotado | diagnóstico sin filtrar secretos |

## `app/config.py`
### Rol del fichero
Modelos Pydantic y validadores de configuración.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| Medium | Architecture | 3 | 4 | 12 | ~878 líneas, 25 clases | dividir por dominio | `config/server.py`, `providers.py`, etc. |
| Positive | Correctness | 4 | 1 | — | validadores para workers, CORS, recursos, IDs | Conservar | tests de config inválida |

## `app/schemas.py`
### Rol del fichero
Contratos Pydantic de API/dominio.

### Hallazgos
| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Maintainability | 4 | 4 | 16 | ~1318 líneas, 62 clases | separar command/query/domain DTOs | módulos por bounded context |
| Medium | Architecture | 3 | 3 | 9 | contratos HTTP y dominio muy próximos | evitar dependencia circular futura | capa de tipos de dominio estable |

# Hallazgos transversales

## Arquitectura
La arquitectura tiene boundaries conceptuales claros, pero físicamente varias fronteras convergen en “mega-módulos”. El mayor riesgo no es ausencia de capas, sino **erosión de cohesión** por crecimiento. La primera modernización debe extraer estrategias del coordinador, repositorios por agregado y routers del dashboard.

## Fiabilidad/resiliencia
Hay señales de diseño cuidadoso: timeouts, reintentos, cuarentena, idempotencia, recuperación de tareas y transacciones. El contrapunto es el volumen de `except Exception` (65 ocurrencias en `app/`): algunos están justificados como best-effort, pero deben convertirse en una taxonomía observable para evitar degradaciones silenciosas.

## Rendimiento
El proyecto es I/O-heavy y usa `asyncio.to_thread` en varios accesos SQLite/bloqueantes. Correcto como puente, pero la carga futura puede quedar limitada por SQLite + RLock + un único workflow configurado. No hay evidencia suficiente para recomendar una DB remota hoy; sí conviene instrumentar queue latency, DB lock time y provider wait.

## Seguridad
Fortalezas: fail-closed para LAN, CSRF, credenciales por env/keyring, sandbox sin red, sin `shell=True`, sin `eval/exec` en `app/`, SQL mayoritariamente parametrizado. Riesgos: config operativa versionada, MCP ejecuta binarios configurados con entorno heredado, y el opt-out de LAN sin auth es deliberadamente peligroso.

## Observabilidad
Existe logging configurado y eventos de dominio, además de trazas de decisiones. Falta evidencia de métricas/tracing estándar exportables (OpenTelemetry/Prometheus). Para un proyecto académico/operativo, instrumentar latencia de cola, selección, inferencia, ingesta, errores por código y saturación de recursos aumentaría mucho la explicabilidad.

## Tests
La suite es extensa y CI exige `coverage.fail_under=90`. Eso es una fortaleza. No se ejecutó la suite, por lo que no se afirma que esté verde ni que la cobertura real alcance 90%.

## Configuración
Hay separación lógica config/código, pero el fichero versionado mezcla defaults, máquina del autor, inventario de modelos y evidencia temporal. Debe existir una configuración de ejemplo portable y un estado local ignorado.

## Dependencias
El lock está fresco (versiones 2026), no “antiguo”. El problema es de política: metadata abierta + lock exacto sin hashes ni mecanismo declarado de regeneración. Además, Python 3.10 está al borde del EOL.

# Estándares recomendados

- Python soportado explícitamente: conservador 3.12–3.14; modernización 3.14.
- Ruff/Mypy target = runtime mínimo real.
- Lockfile generado por herramienta y verificado con hashes.
- Routers FastAPI por dominio; coordinador como façade sobre estrategias.
- Repositories por agregado; migraciones versionadas.
- Excepciones por taxonomía (`ProviderError`, `IngestionError`, etc.) y catches amplios solo en fronteras top-level con log/metric.
- Logging estructurado con `correlation_id/task_id/invocation_id/provider/model`.
- ADRs para: SQLite, routing adaptativo, sandbox Docker, frontera de datos/MCP.
- CI: lint, types, tests, coverage, dependency audit, secret scan, build/package check.
- PR template con impacto de contratos, migraciones y config.

# Roadmap

## Quick wins
1. Subir mínimo Python a 3.12 y alinear Ruff/Mypy/CI.
2. Introducir `broker_config.example.yaml`; ignorar config local/estado descubierto.
3. Añadir dependency audit y pin de acciones por SHA.
4. Documentar generación del lockfile.
5. Añadir logging a catches silenciosos de startup/maintenance.

## Medio plazo
1. Extraer routers de `main.py` y `dashboard_web.py`.
2. Partir `coordinator.py` por estrategia.
3. Partir `repository.py` y `schemas.py` por dominio.
4. Migraciones versionadas.
5. Métricas estructuradas para colas, DB, providers, ingesta.

## Largo plazo
1. Boundaries de dominio explícitos y contratos internos.
2. OpenTelemetry/Prometheus si el proyecto necesita operación prolongada.
3. Contract tests reales por provider.
4. Evaluar DB alternativa solo si métricas demuestran contención/escala insuficiente.

# Tareas para ejecución

| ID | Título | Archivos | Prioridad | Severidad/Riesgo | Esfuerzo |
|---|---|---|---|---|---|
| AUD-01 | Retirar Python 3.10 | `pyproject.toml`, CI, Docker/docs | P0 | High/15 | S |
| AUD-02 | Separar config portable de config local | `broker_config.yaml`, `.gitignore`, docs | P0 | High/15 | M |
| AUD-03 | Añadir audit de dependencias y secret scan | CI | P0 | Medium/9 | S |
| AUD-04 | Formalizar lockfile | `requirements.lock`, docs/CI | P1 | Medium/12 | M |
| AUD-05 | Extraer estrategias del coordinador | `app/coordinator.py` + nuevos módulos | P1 | High/20 | L |
| AUD-06 | Partir dashboard web/forms | `dashboard_web.py`, `dashboard_forms.py` | P1 | High/16 | L |
| AUD-07 | Partir repositorio/schema por dominio | `repository.py`, `schemas.py` | P1 | High/16 | L |
| AUD-08 | Versionar migraciones SQLite | `db.py` + migraciones | P2 | Medium/12 | M |
| AUD-09 | Taxonomía de catches amplios | múltiples `app/*.py` | P2 | High/16 | M |
| AUD-10 | Métricas/tracing exportable | logging/health/coordinator/providers | P2 | Medium/9 | M |

# Supuestos y límites del análisis

- Análisis por lectura estática; **no se ejecutó código**.
- No se verificó el estado real de Docker, Ollama, LM Studio, ffmpeg, keyring ni GPU.
- No se validaron datos/migraciones contra una base SQLite real.
- No se ejecutó `pytest`, Ruff, Mypy, coverage, Bandit, pip-audit ni secret scanning.
- La lista de hallazgos por fichero prioriza ficheros críticos; el proyecto contiene 51 módulos Python bajo `app/` (~23.9k LOC). La modernización propone fases por módulo para completar refactors sin un “big bang”.
