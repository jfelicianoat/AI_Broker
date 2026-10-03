# System-1 en AI_Broker

## Arquitectura y plan previo

El broker usa FastAPI (`app/main.py`), configuración Pydantic/YAML, SQLite y
logs JSON. `RoutedModelProvider` aplica filtros de proveedor, privacidad,
capacidades, compatibilidad y contexto y después ordena por evidencia de
latencia. La selección explícita tiene prioridad. `OllamaProvider.generate`
ya soporta JSON Schema, gestión de memoria, leases y cancelación asyncio.
`MCPRegistry` mantiene procesos stdio; Laya ya está configurado localmente.

Plan: reutilizar ese registro MCP y el adapter Ollama; añadir contrato de
juicio, perfiles centralizados, validación estricta, fallback y telemetría;
incorporar clasificación simple/media/compleja después de los filtros,
asignando capacidad mediante reglas explícitas configurables (sin inferir
calidad del tamaño del modelo); conservar selección previa en shadow mode;
verificar regresiones, fallos y ambos proveedores reales; guardar benchmark
con etiquetas esperadas antes de habilitar routing efectivo.

Las apps conservan las reglas deterministas de su negocio y usan el juicio
solo cuando `accepted=true`. `decision=null` indica que deben continuar con
su comportamiento previo.

## Implementación final

Por petición del usuario, el orden final es **Nimble (`nimble:latest`) →
Laya mediante MCP → comportamiento previo**. Esto sustituye la prioridad
inicial del documento de encargo. Ambos proveedores reales se han probado.

- `POST /api/v1/system1/judge`: binario, choice de 2–20 etiquetas o score
  ordinal de 2–20 niveles. Requiere la autenticación admin existente.
- `GET /api/v1/system1/metrics`: contadores y últimas 1.000 muestras,
  protegidos con la misma credencial; logs JSON persistentes sin el input.
- `/api/v1/capabilities` anuncia si juicio y routing semántico están activos.
- `System1Service` centraliza timeout por intento, cancelación, validación,
  umbrales, margen, prioridad y causas de fallback. Si se rechaza una salida,
  `accepted=false` y `decision=null`; nunca entrega la decisión corrupta.
- El registro MCP existente aporta un resultado estructurado sin el recorte
  de 8.000 caracteres del renderizado agéntico. Serializa lectores y reinicia
  un proceso cancelado para evitar cruzar respuestas.
- Ollama usa `/v1/systemone` cuando el catálogo declara `decision`, reutiliza
  cliente HTTP y leases de memoria, y admite la generación con JSON Schema
  para modelos ordinarios. No se elige el transporte por el nombre del modelo.
- La normalización de Laya y del endpoint nativo usa el score top1 de la
  distribución completa. El campo nativo `confidence` es concentración por
  entropía, no probabilidad de acierto. La respuesta del broker siempre marca
  `confidence_is_calibrated=false` para estos casos no calibrados.
- Para `score`, `decision` es el índice ordinal de mayor score (base cero),
  con las demás puntuaciones en `alternatives`; no mezcla confianza top1 con
  el valor esperado fraccional que devuelven los proveedores.

Referencia del endpoint nativo: [Nimble en Ollama](https://ollama.com/library/nimble)
y [contrato oficial de System One](https://github.com/ollama/ollama/blob/main/docs/openapi.yaml).
La versión local comprobada es Ollama **0.35.0**; Laya **0.3.20**.

## Configuración

Todo vive en `system1` de `broker_config.yaml`, con modelos Pydantic en
`app/config.py` y valores por defecto desactivados para instalaciones antiguas.

| Clave | Valor configurado |
|---|---|
| `enabled` | `true` |
| `provider_priority` | `[ollama_system1, laya_mcp]` |
| `timeout_seconds` | `30.0` por intento, incluye espera/carga |
| `fallback_policy` | `provider_then_current`; `current` evita el segundo proveedor. El segundo solo se usa si el primero **falla**, no si duda |
| `ollama.model` | `nimble:latest` |
| `ollama.max_output_tokens` | `512`, solo generación ordinaria, no endpoint nativo |
| `ollama.keep_loaded` | `true`: Nimble queda residente (no se descarga por inactividad ni ocupa hueco de modelo) |
| `laya.server_id/tool/model` | `laya/laya_predict/multilingual` |
| `laya.max_input_tokens` | `300` (cota superior estimada); por encima, `INPUT_TOO_LARGE` |
| `thresholds.goal_completion.confidence` | `0.97` |
| `thresholds.semantic_routing.confidence/min_margin` | `0.90/0.15` |
| `thresholds.ranking.confidence/min_margin` | `0.85/0.15` |
| `routing.enabled/shadow_mode` | `true/true` |

Los perfiles admiten instrucciones predeterminadas; un caso nuevo puede enviar
`instructions`. Si se omiten y no existe perfil con instrucciones, el broker
devuelve `MISSING_INSTRUCTIONS`. Un `threshold_profile` desconocido se rechaza
con fallback, para que una errata no rebaje silenciosamente el umbral.

Los tiers iniciales son experimentales: `gemma4:12b` simple,
`qwen3-coder:30b` medium, `ornith-1.5:35b` complex. Son reglas sobre identidad
`proveedor/deployment/modelo`; no se deduce calidad del número de parámetros.
Se deben validar contra calidad real antes de habilitar el routing efectivo.

## Routing semántico

Se aplica a la selección automática de una inferencia `single` tras los filtros
existentes de privacidad, proveedor, compatibilidad, visión y contexto, y tras
comprobar el presupuesto declarado. La clasificación no puede reincorporar un
modelo eliminado. Los modelos `decision` se apartan de la generación normal:
Nimble clasifica, pero no redacta la respuesta del usuario.

Se mantienen modelos explícitos, árbitros, contratos multimodelo y embeddings.
`auxiliary_invocations=false` evita la llamada de clasificación. Si no hay al
menos dos tiers conocidos, se conserva el router previo. Con un juicio aceptado,
el modo efectivo escoge el menor tier suficiente y conserva el ranking existente
dentro de ese tier. Con poca confianza, margen insuficiente o proveedores caídos,
devuelve el orden previo. El modo sombra registra modelo actual y propuesto y
devuelve el actual.

Ejemplos comprobados contra el catálogo real de Ollama, en el broker de prueba:

| Petición | Nivel | Modelo propuesto/efectivo |
|---|---|---|
| Capital de Francia | simple | `gemma4:12b` |
| Validar CSV, quitar duplicados y sumar por categoría | medium | `qwen3-coder:30b` |
| Demostrar que un grupo de orden primo es cíclico | complex | `ornith-1.5:35b` |

Cada ejemplo se ejecutó en sombra y en modo efectivo. La evidencia del modelo
actual, el propuesto, la confianza y la latencia está en `benchmark_system1.json`.
La configuración del broker en ejecución permanece en sombra.

## Petición y respuesta real

El broker reiniciado ha respondido a esta petición en su API HTTP habitual,
conservando su credencial de sesión. La evidencia completa está en
`live_broker_system1.json`.

```json
{
  "use_case": "ranking",
  "decision_type": "binary",
  "input": {"message": "I request a refund."},
  "instructions": "Does message request money back?"
}
```

Campos de la respuesta observada, sin los detalles de intento:

```json
{
  "use_case": "ranking",
  "decision": true,
  "confidence": 0.9992176991620464,
  "confidence_is_calibrated": false,
  "alternatives": [{"value": false, "confidence": 0.0007823008379536089}],
  "provider": "ollama_system1",
  "model": "nimble:latest",
  "latency_ms": 170.558,
  "fallback_used": false,
  "reason_code": null,
  "accepted": true
}
```

Esos `170.558` ms son de la llamada del 1 de octubre con Nimble ya cargado
(consta en `logs/ai-broker.log`). `live_broker_system1.json` se regeneró
después con una llamada en frío y guarda `12445.753` ms: la misma decisión,
más la carga del modelo.

No ejecutar reglas de negocio mirando solo `decision`: comprobar primero
`accepted`. La aplicación debe aplicar sus reglas deterministas antes de pedir
este juicio y recuperar su flujo previo si la decisión no se acepta.

## Tests y calibración inicial

Suite completa tras la revisión del 2 de octubre: **970 passed, 4 skipped,
2 subtests passed** (la entrega original dejaba un fallo; ver la revisión al
final). También
correctos `ruff check` y `mypy app` (54 módulos). La advertencia existente es
la deprecación de httpx en Starlette TestClient; no es un fallo de estos cambios.

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .local\system1-check -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check app tests scripts\benchmark_system1.py scripts\inspect_system1.py
.\.venv\Scripts\python.exe -m mypy app
.\.venv\Scripts\python.exe -m scripts.benchmark_system1 --repeats 2
```

La carpeta temporal dentro del proyecto evita el acceso denegado al directorio
pytest temporal del usuario en el entorno aislado. Laya necesita permiso para
ejecutar su Python instalado fuera del aislamiento; no se modificó su instalación.

Los tests incluyen ambos órdenes de prioridad, proveedores caídos, JSON inválido,
confianza/margen, timeout y cancelación, restricciones deterministas, feature off,
privacidad, autenticación, modo sombra, optout de auxiliares, API nativa de Nimble
y flujo JSON de Ollama ordinario. Los tests MCP usan un subproceso real para
verificar concurrencia, respuestas sin truncar y recuperación tras cancelación.

El benchmark tiene **14 casos etiquetados antes de ejecutarlos, 2 repeticiones
por proveedor: 56 llamadas reales**. Incluye 9 tareas de routing, 4 de completitud
y 1 score ordinal. Las etiquetas son representativas, no datos de producción.

| Métrica, muestra completa | Laya MCP | Nimble Ollama |
|---|---|---|
| Accuracy sin aplicar umbrales | 64,3% | 85,7% |
| Accuracy en decisiones aceptadas | 100% | 100% |
| Rechazo/fallback con umbrales de producción | 85,7% | 14,3% |
| Estabilidad de la etiqueta entre repeticiones | 100% | 100% |
| Down-routing inseguro aceptado en esta muestra | 0 | 0 |

Latencias, p50/p95, precision/recall/F1 por clase, falsos positivos y negativos,
consumo de tokens, alternativas y todas las respuestas se conservan en
`benchmark_system1.json`. La carga inicial de Laya cuesta unos 7 segundos y
aparece en su primera llamada; las siguientes cuestan decenas de milisegundos.
El benchmark devuelve `null` para métricas que el proveedor no expone.

Nimble acierta los cuatro ejemplos de completitud; Laya da falsos positivos
en los dos ejemplos incompletos. El umbral `0.97` los rechaza (desde la revisión, una duda del primario ya no pasa al segundo proveedor) y permite probar
el siguiente proveedor o volver al comportamiento previo. En routing, Nimble
acierta las tres tareas complejas; ambos tienen dudas en casos medios.

Se verificó además fallback real en ambas direcciones, poniendo deliberadamente
un identificador de proveedor/modelo inexistente solo en el broker de prueba.
La respuesta de Nimble primario y el fallback Nimble → Laya están guardados como
`configured_primary_example` y `live_nimble_to_laya_fallback` en el benchmark.

## Archivos modificados y nuevos

- `app/config.py`, `app/schemas.py`: configuración y contratos.
- `app/system1.py`, `app/providers/system1.py`: servicio y adapters.
- `app/providers/ollama.py`: endpoint nativo con cliente y lifecycle existentes.
- `app/providers/routing.py`: filtros previos y punto de selección semántica.
- `app/main.py`: construcción compartida, API protegida y capacidades.
- `app/mcp.py`: resultado estructurado y recuperación/concurrencia de stdio.
- `app/logging_config.py`, `app/dashboard_forms.py`: logs y configuración compartida.
- `broker_config.yaml`: sección System-1, Nimble primario y modo sombra.
- `tests/test_system1.py`, `tests/test_mcp.py`, `tests/test_auth_surface.py`,
  `tests/fixtures/mcp_echo_server.py`: cobertura de contratos, fallos y transporte.
- `scripts/benchmark_system1.py`, `scripts/inspect_system1.py`: pruebas reproducibles.
- Esta carpeta: validación etiquetada, informe, benchmark y evidencia HTTP en vivo.

## Límites y siguientes validaciones

La integración está implementada y verificada, pero esta muestra pequeña no
declara estable la política semántica ni la completitud de objetivos reales.
Antes de quitar `shadow_mode`, ampliar ejemplos de producción, validar los tiers
de los modelos de respuesta y comparar su calidad/coste/tiempo de generación.
Los ahorros de tokens, tiempo y llamadas de generación todavía no se han medido:
el modo sombra conserva el routing real y añade el coste del clasificador. No se
afirma calibración probabilística, por lo que no se presentan Brier/ECE.

No se han modificado otras aplicaciones del TFM: pueden integrar el contrato
centralizado del broker sin necesitar un cliente Laya/Ollama propio. No se ha
añadido una base de datos, servicio ni bus paralelo. Los cambios existentes en
configuración, copias y logs previos al trabajo se conservaron.

## Revisión del 2 de octubre de 2026

Revisión independiente del código, las cifras y la evidencia. Las cifras del
benchmark se recalcularon desde las 56 filas de `benchmark_system1.json` y
coinciden con la tabla de arriba. `/v1/systemone` y la capacidad `decision` de
`nimble:latest` se comprobaron contra Ollama 0.35.0 en esta máquina.

### Defectos corregidos

| Defecto | Efecto | Corrección |
|---|---|---|
| `tests/test_long_context.py` seguía esperando el contrato `2.10` | La suite daba 1 fallo, no «962 passed» | Aserción actualizada a `2.11` |
| Laya trunca en silencio a 512 tokens (pregunta incluida) | Con un prompt de 57 KB Nimble rechaza (`MODEL_ERROR`, su contexto es de 8.194 tokens y no trunca) y Laya devolvía `simple` con 0,939: **juicio aceptado** que en modo efectivo mandaría la tarea al tier más bajo | `system1.laya.max_input_tokens` (300): por encima, `INPUT_TOO_LARGE` y se conserva el router previo |
| El filtro de presupuesto vivía en `eligible_catalog` | Con `routing.enabled` cambiaba el routing real **también en modo sombra**, y un `target_model` cloud sin precio con `max_cost_usd` fallaba con un `CONTEXT_LIMIT_EXCEEDED` falso | El presupuesto solo acota los candidatos que se pasan a System-1; sombra, targets, mixture y sondeo quedan como antes |
| Cualquier error JSON-RPC de `tools/call` mataba el proceso MCP | Un argumento inválido de un agente obligaba a recargar Laya (7–17 s) | Solo se reinicia ante cancelación, timeout o canal roto (`MCPToolError` no reinicia) |
| `routing.instructions` y las de los perfiles sin límite | Más de 4.000 caracteres rompían la selección de modelo con un `ValidationError` | Límite validado al cargar la configuración |

### Segunda pasada: pendientes resueltos

| Pendiente | Solución |
|---|---|
| Carga en frío de Nimble (8,4 GB, ~12 s) antes de cada tarea tras un rato parado | `system1.ollama.keep_loaded: true`: no se descarga por inactividad ni gasta hueco del cupo de modelos; su memoria sí cuenta y sale el último si hace falta el sitio |
| Los juicios hacían cola tras la generación (semáforo serie) y agotaban el plazo | El endpoint nativo de decisión ya no toma el semáforo; un modelo ordinario que genera su juicio sí. En vivo: 4 juicios durante una generación de `ornith:35b`, 0,1–0,2 s cada uno |
| Una errata en `use_case` rebajaba el umbral al valor por defecto | `UNKNOWN_USE_CASE` salvo que se pida `threshold_profile: "default"` |
| Los tests escribían en `logs/ai-broker.log` | `tests/conftest.py` desvía a un directorio temporal toda configuración que apunte al `logs/` real |
| El sondeo marcaba `incompatible` los modelos de `unsloth` por «No model loaded» | Ese 400 describe el estado del servidor: ahora es `error` (se reintenta). Los 9 vetos del YAML vuelven a `unknown` |
| Duda del primario «rescatada» por el segundo proveedor | Visto en la primera tarea real en sombra: Nimble daba `LOW_CONFIDENCE` y Laya aceptaba `simple` a 0,957. `LOW_CONFIDENCE` e `INSUFFICIENT_MARGIN` cortan ahora la cadena; el segundo proveedor solo cubre fallos |

El broker se reinició con su token de sesión conservado y todo lo anterior se
comprobó por su API HTTP. Copia previa de la configuración:
`broker_config.yaml.bak-20261002-system1`.

### Lo que sigue necesitando datos, no código

- **Modo efectivo.** Con un juicio aceptado solo se eligen modelos con tier
  declarado (hoy tres locales). Es la opción conservadora —un modelo sin tier
  no ha demostrado nada—, pero hay que declarar más tiers antes de activarlo.
- **Política semántica.** Solo hay dos eventos `system1.routing` reales, los
  de esta revisión. Quitar `shadow_mode` sigue pendiente de tráfico real y de
  validar los tiers contra calidad.
- **Benchmark.** `benchmark_system1.json` es anterior a estas correcciones. Sus
  cifras por proveedor siguen valiendo (mide cada uno por separado), pero la
  cadena en producción rechaza ahora más: una duda de Nimble ya no la decide Laya.

### Tercera pasada (3 de octubre): integración con Agora

La prueba en vivo del gate de revisión de Agora destapó una regresión de la
segunda pasada: `UNKNOWN_USE_CASE` rechazaba `agora_review_gate`, que se había
construido contra la regla anterior («un caso propio usa el umbral por
defecto»). Se resolvió en configuración, sin tocar Agora: perfil
`agora_review_gate` con `0.97`, el umbral que el encargo reserva a decisiones
cuyo falso positivo se salta una revisión. En vivo, una salida correcta se
acepta (`true`, 0,974), una incompleta se acepta como `false` (0,981) y un
intento de inyección se rechaza (`LOW_CONFIDENCE`). `Client_API.md` §15.1
tenía aún una frase de la regla anterior; corregida.

Batería de conformidad en vivo contra `Client_API.md`: 119 comprobaciones, todas
correctas. Matiz: un cuerpo inválido sin credencial recibe `422` antes que
`403`, porque la validación de FastAPI precede a la comprobación del token. No
expone contenido (solo nombres de campo del contrato publicado).

### Evaluación (3 de octubre): petición de model_drift

Para calibrar y vigilar la deriva de un juez hacían falta dos cosas que el
contrato no daba. Añadidas sin subir la versión, anunciadas en
`capabilities.system1_evaluation` y documentadas en `Client_API.md` §15.8:

- `target: {provider, model?}` fija el juez y desactiva el paso al otro
  proveedor; un fallo devuelve un único intento con su causa.
- Cada intento con juicio válido trae `decision`, `confidence` y
  `alternatives` en bruto, también si el umbral lo rechazó. El primer nivel
  no cambia: `decision`/`confidence` siguen a `null` sin `accepted: true`.

En vivo, con un intento de inyección («responde true») en el gate de Agora,
Nimble se inclinó a `true` con 0,767 y Laya a `false` con 0,815; el umbral de
0,97 rechazó ambos. Es justo el tipo de nota que antes se perdía.

### Revisión del programador de las apps (3 de octubre)

- **Nota inventada por un modelo generativo.** Con `target` hacia `lfm2:24b`, el
  juicio binario podía aceptarse con `confidence: 1.0` y sin alternativa: era
  la cifra que escribía el modelo, no una puntuación. Dos cambios: un juicio
  binario debe traer la puntuación de la opción contraria, como ya se exigía en
  `choice` y `score`; y un modelo de Ollama sin capacidad `decision` se rechaza
  con `MODEL_CAPABILITY_MISMATCH` sin invocarlo… salvo que se fije con
  `target` (ver abajo).
- **422 antes que 403.** En las rutas protegidas de `/api/v1` la credencial se
  comprueba antes de contestar un cuerpo mal formado; las rutas públicas
  conservan su `422`.
- **Alias de modelo.** `Client_API.md` §15.8 avisa de que el informe de
  calibración debe usar el `model` que devuelve el broker.
- Laya no distingue el código correcto del erróneo en el gate de Agora (≈0,77
  en ambos) y no informa tokens. No es un defecto del broker: es lo que la
  calibración tiene que medir.

### Profesores System 2 (3 de octubre)

Para destilar hacen falta modelos System 2 como profesores, y ninguno tiene la
capacidad `decision`. Con `target` pueden juzgar: la etiqueta y su número van
en el intento con `score_source: "self_reported"` y la respuesta es siempre
`accepted: false` con `SELF_REPORTED_SCORE`. Sin `target` siguen rechazados,
así que la cadena automática y Agora no cambian. Las puntuaciones de Nimble y
Laya se marcan `native`. Se retiró `allow_generated_scores`: con esta regla ya
no tenía uso. Pendiente de validar en las pruebas de Model_Drift.

En la primera prueba en vivo `lfm2:24b` contestaba `"alternatives": []` o
repetía su decisión como alternativa, y todo salía `INVALID_OUTPUT`. Al modelo
generativo se le pide ahora un mapa `scores` con todas las etiquetas
obligatorias (la gramática de Ollama impide omitir o repetir ninguna), y el
broker construye la distribución a partir de él. En vivo, 12 de 12 juicios de
`lfm2:24b` válidos y marcados `self_reported`: acierta el binario y el choice;
en el score puntúa «regular» un código correcto que Nimble puntúa «bien».
Con `temperature: 0` su número varió entre 1,0 y 0,7 en el mismo caso.
