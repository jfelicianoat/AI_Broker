# Probador de Prompts del AI Broker

## Estado y fase

Funcionalidad planificada para la **fase 5 — Dashboard operativo**. Este documento define el alcance; todavía no existe implementación de UI ni endpoints específicos del probador. La lógica común de las tres pantallas y sus prerrequisitos están en [`Phase_5_Dashboard.md`](Phase_5_Dashboard.md).

## Objetivo

Permitir probar manualmente un prompt contra un modelo concreto o contra una selección concreta de `mixture_of_agents`, usando exactamente la misma API, cola, límites, persistencia y seguridad que cualquier otro cliente del Broker.

El probador es una interfaz de diagnóstico. No crea workflows de conocimiento, no consulta Internet, no modifica Obsidian y no llama directamente a Ollama o DeepSeek.

## Modos de entrada

### Prompt libre

Editor de texto multilínea. El contenido se copia sin reescritura a `content.prompt`.

### JSON

Editor de JSON con validación sintáctica previa. El JSON es **contenido para el LLM**, no un request administrativo del Broker. El texto original —incluidos orden, espacios y saltos— se conserva en `content.prompt`; parsearlo sirve únicamente para detectar errores de sintaxis y no autoriza al Broker a interpretar su semántica.

En ambos modos se muestran longitud, cota conservadora de contexto y errores antes del envío. No se trunca automáticamente.

## Estrategias de ejecución

### Modelo único

- `execution.strategy = single`.
- El usuario elige una referencia exacta `provider + deployment + model` del catálogo descubierto.
- La selección exacta usa la referencia contractual `target_model` con `provider + deployment + model`; `preferred_model` se conserva para compatibilidad, pero no basta para distinguir destinos homónimos.
- La UI desactiva sustitución cuando se solicita probar exactamente ese modelo.
- Puede habilitarse fallback explícitamente; nunca se activa de forma implícita.

### Mixture of LLMs determinado

- `execution.strategy = mixture_of_agents`.
- `preset = fast` usa ejecución serial; `preset = slow` permite proponentes paralelos o por oleadas. Ambos usan `selection.mode = manual` en el probador.
- El usuario selecciona de uno a cinco proponentes, sus etiquetas de rol y un árbitro exacto.
- Cada referencia incluye proveedor, deployment y modelo para distinguir local de cloud.
- La UI valida modelos disponibles, referencias completas, política cloud, presupuesto, quórum, timeout y contexto antes de encolar.
- En `fast`, proponentes y árbitro se ejecutan uno a uno. En `slow`, los proponentes pueden solaparse dentro de una única tarea y el árbitro espera a la barrera. Los roles son metadata auditable y no provocan que el Broker reescriba el prompt.
- Presets `standard`, `verified` y `high_stakes` solo se habilitarán cuando estén implementados en el coordinador; no aparecerán como opciones funcionales antes.

## Controles

- Tipo de entrada: `Prompt` o `JSON`.
- Estrategia: `Modelo único` o `Mixture of LLMs`.
- Catálogo filtrable por proveedor, deployment, capacidad y contexto.
- Modelo único o lista ordenada de proponentes más árbitro.
- Temperatura, máximo de tokens, formato de salida y JSON Schema opcional.
- Clasificación de datos, autorización cloud, proveedores permitidos, fallback, timeout y coste máximo.
- Acciones `Validar`, `Encolar`, `Cancelar` y `Repetir como nueva prueba`.

## Ejecución y persistencia

`Encolar` construye un `TaskCreateRequest` v2 normal y llama a `POST /api/v1/tasks`. No se crea un canal privilegiado:

- la tarea queda en la cola durable;
- solo existe un workflow activo global;
- se aplican semáforo, VRAM, contexto, coste, cancelación e idempotencia;
- el origen se marca únicamente como metadata opaca `origin = prompt_tester`;
- cada ejecución usa una nueva clave idempotente, salvo repetición explícita del mismo request.

El historial se obtiene de las tareas persistidas, no de estado exclusivo del navegador.

## Resultado visible

La pantalla muestra sin interpretar:

- estado, posición, fase y tiempo transcurrido;
- contenido raw o embedding;
- modelo efectivo y si hubo fallback;
- tokens, coste y latencia;
- para mixture: proponentes, árbitro, plan solicitado/efectivo, waves, concurrencia observada, advertencias y metadata de consenso disponible;
- request final validado y respuesta técnica para copiar.

El HTML escapa siempre prompts y respuestas. El contenido del LLM nunca se inserta como HTML confiable. No se muestran secretos, cabeceras de autorización ni cadenas privadas de razonamiento.

No se muestran porcentajes de generación, tokens/s, confianza ni atribución de candidatos si el provider o el contrato no producen esos datos. `output.language` se trata como metadata y no como una orden que el Broker añada al prompt.

## Errores esperados

La UI presenta códigos tipados como `CONTRACT_VALIDATION_FAILED`, `MODEL_UNAVAILABLE`, `MODEL_CAPABILITY_MISMATCH`, `CONTEXT_LIMIT_EXCEEDED`, `BUDGET_EXCEEDED`, `PROVIDER_UNAVAILABLE`, `CONSENSUS_QUORUM_NOT_REACHED` y `CANCELLED`, conservando el request para corregirlo y volver a enviarlo.

## Criterios de aceptación

1. Un prompt libre llega al provider sin modificaciones.
2. Un JSON inválido no crea una tarea; uno válido conserva exactamente el texto introducido.
3. El modo single usa el modelo exacto seleccionado o falla si fallback está desactivado.
4. El modo mixture persiste los proponentes, etiquetas de rol y árbitro elegidos; `fast` es serial y `slow` demuestra solapamiento real cuando el plan efectivo es paralelo.
5. Las pruebas comparten cola y nunca ejecutan LLMs fuera del worker normal.
6. Una generación lenta no bloquea la interfaz; puede consultarse y cancelarse.
7. Recargar el navegador conserva el historial y los estados mediante SQLite.
8. Prompts y respuestas con HTML o instrucciones hostiles se muestran como texto seguro.
9. Privacidad, cloud, presupuesto y contexto se validan igual que en la API pública.
10. Las pruebas automáticas cubren construcción de requests, validación JSON, selección manual, XSS, cancelación y recuperación tras reinicio.
11. El timeout elegido limita realmente la ejecución completa y produce un error tipado.
12. Ningún dato visible se simula: las métricas proceden de tareas, invocaciones o health checks con timestamp.
13. `slow` nunca activa un segundo workflow Broker y nunca supera el límite de invocaciones, VRAM, presupuesto o cuotas reservado para su wave.

## Base backend completada

- `target_model` se valida por identidad completa y vuelve a comprobarse justo antes de invocar.
- `execution.timeout_seconds` limita la tarea completa y cancela las operaciones provider pendientes con `TASK_TIMEOUT`.
- El progreso separa `budget_limit_usd`, `cost_estimated_usd` y `cost_actual_usd`; un límite no se presenta como coste reservado.
- `fast` conserva concurrencia uno y `slow` puede lanzar proponentes concurrentes dentro del único workflow activo.
