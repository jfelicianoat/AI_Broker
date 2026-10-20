# Integración Broker–LARM — estado

Última actualización: 2026-10-10. Prompt: **P107 — Cambio compañero en AI Broker: LocalRuntimeResolver real** (paquete LARM v3, contexto `Contexto_Broker_P107_v3`).

## Qué hay en este repositorio

| Pieza | Dónde | Contrato |
|---|---|---|
| Configuración `larm` (apagada por defecto, solo loopback) | `app/config.py` · `LarmConfig` | LARM 06 |
| Cliente de la API LARM, canal de credenciales, diario de leases y `LocalRuntimeResolver` | `app/larm.py` | LARM 04, 06, 07 |
| Provider interno `larm` (lease → protocolo OpenAI → renovación → liberación) | `app/providers/larm.py` | LARM 06, spec §62–§64 |
| Integración en el router (solo target exacto, admisión de contexto delegada) | `app/providers/routing.py` | LARM 06 |
| Clave idempotente `task_id:invocation_id` | `app/invocation_context.py`, `TaskRepository.start_invocation` | LARM 06 |
| Extensión versionada `larm.v1` de la huella | `app/execution_fingerprint.py`, `ExecutionFingerprint.extensions` | LARM 05 |
| Capacidades aditivas `local_runtime_resolver`, `larm_contract_version` | `app/main.py`, `BrokerCapabilitiesResponse` | LARM 06 |
| Reconciliación de leases al arrancar (tabla `larm_leases`) | `app/main.py`, `app/db.py` | LARM 06 |
| Frontera de datos: `larm` es proveedor local; endpoint de lease fuera de loopback = `CLOUD_NOT_ALLOWED` | `app/schemas.py`, `app/providers/larm.py` | Client_API 2.11 §4 |
| Contract tests (LARM simulado validado contra los schemas v3) | `tests/test_larm_bridge.py`, `tests/test_larm_bridge_edges.py`, `tests/larm_fake.py`, `tests/fixtures/larm/` | — |
| Runner de integración real | `scripts/larm_p107_real.py` | P107 |
| Documentación de cliente y operador | `docs/Client_API.md` §2, §8.2, §12, §16 · `docs/Configuration_Reference.md` | — |

`contract_version` sigue en `"2.11"`: todo es aditivo y ninguna petición existente cambia.

## Estado: BLOCKED_REAL

| Puerta | Resultado |
|---|---|
| `ruff check .`, `mypy app`, `compileall` | PASS |
| `pytest -q` (suite completa) | 1071 passed, 4 skipped, 0 failed |
| Contract tests del puente (LARM simulado validado contra los schemas v3) | 50 passed (incluye un named pipe real de Windows) |
| Cobertura (`fail_under = 90`) | 88,97 %: rojo, ya lo estaba antes (87,86 % en main); módulos nuevos al 94 % y al 98 % |
| Prueba real `scripts/larm_p107_real.py` (Broker aislado → larm-daemon → Ollama `gemma4:12b`) | 8/8 PASS: served_by exacto, artefacto final con SHA-256 verificado, backend GPU verificado, 4 renovaciones y liberación, cancelación que libera, variante desconocida sin fallback |

Por qué no es REAL_PASS:

- P099 y P101 de LARM no existen todavía. El servidor LARM es una rebanada de adopción externa con el contrato exacto, no P100/P101.
- El cliente corrió en este PC y no en el PC principal.
- `tools/check.ps1 -Prompt P101 -Real` no tiene pruebas registradas.

Con `larm.enabled: false` (el valor por defecto) el broker de producción no cambia de comportamiento.

Informe y evidencia, en el repositorio LARM: `docs/reports/P107_cambio-companero-en-ai-broker-localruntimeresolver-real.md`, su `.evidence.json` (schema `PromptEvidence`) y la evidencia bruta en `docs/reports/P107_real/`.

## Cómo repetir la prueba real

```
cargo build -p larm-daemon          # en el repositorio LARM
python scripts/larm_p107_real.py --larm-repo "<ruta>/larm" --model gemma4:12b --out "<ruta>/larm/docs/reports/P107_real"
```

El runner da de alta un principal temporal (token por stdin al Credential Manager), usa el puerto 8766 y una base de datos propia, no publica el token del broker de producción y al terminar borra la credencial. Desde el PC principal, ejecuta la parte cliente contra el broker del PC IA: es el pendiente que cierra la prueba obligatoria.
