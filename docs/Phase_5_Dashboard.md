# Fase 5 — Contrato operativo del dashboard

## Estado

Contrato lógico y estado de implementación del dashboard. La pantalla de Operación está implementada en la fase 5.2; Probador y Comparación permanecen planificados para las fases 5.3 y 5.4.

## Frontera de responsabilidad

El dashboard es un cliente del AI Broker. Puede consultar estado, encolar una inferencia ya preparada, reordenar tareas pendientes y solicitar cancelación. No llama directamente a Ollama o DeepSeek, no crea workflows de conocimiento y no modifica información de Obsidian.

`mixture_of_agents/fast` y `mixture_of_agents/slow` se consideran rutas técnicas compuestas solicitadas explícitamente. El Broker ejecuta el algoritmo versionado —proponentes y árbitro—, pero no decide pasos de negocio, no divide documentos y no inventa prompts por rol. Los roles seleccionados son etiquetas auditables; no alteran el prompt original.

## Invariante de concurrencia

- Solo puede existir un workflow Broker activo.
- `single` y `mixture_of_agents/fast` permiten **una invocación LLM activa**.
- `mixture_of_agents/fast` ejecuta `proponente 1 → … → proponente N → árbitro` de forma serial.
- `mixture_of_agents/slow` puede ejecutar varios proponentes en paralelo dentro del único workflow activo. El árbitro se inicia únicamente cuando termina la barrera de proponentes requerida.
- El paralelismo de `slow` queda acotado por `max_parallel_invocations`, reservas de VRAM, presupuesto, cuotas del provider y timeout. El plan efectivo puede ser `parallel`, `waves` o `sequential`.
- Mientras una invocación está esperando al LLM, la API, el dashboard, el alta de nuevas tareas, el polling y la cancelación continúan respondiendo. Las demás tareas permanecen `queued`.
- Elegir `slow` autoriza paralelismo, pero no obliga a ignorar los límites de seguridad. La UI muestra el plan solicitado y el efectivo.

La base del backend ya lanza concurrentemente los proponentes de `slow` y mantiene `fast` serial. Antes de representar carriles en el dashboard deben persistirse timestamps por invocación y completarse la prueba con providers reales, porque el runtime externo todavía podría serializar las solicitudes.

## Pantalla 1 — Operación

| Elemento | Fuente real | Regla de presentación |
|---|---|---|
| Pendientes y tarea activa | `tasks` y cola durable | Mostrar `N pendientes · 1 slot LLM`; nunca “trabajadores” |
| Latencia p50/p95 | `model_invocations.latency_ms` | Ventana temporal explícita y solo invocaciones terminadas |
| Coste | `model_invocations.cost_usd` | Separar coste real, límite de presupuesto y estimación; `N/D` si faltan tarifas |
| Éxito y completadas | estados terminales de `tasks` | Mostrar periodo y denominador |
| VRAM | snapshot real de Ollama `/api/ps` | Uso observado y hora de comprobación; no inferir GPU total si no está disponible |
| Salud | SQLite, dispatcher y providers | Estado, causa, latencia y `checked_at`; sin estados optimistas por defecto |
| Cola | proyección de `tasks.request_json` | ID, destino solicitado, estado, posición, antigüedad y cancelación |
| Tarea activa | tarea, progreso e invocaciones | Fase e invocaciones completadas; barra indeterminada durante una llamada individual |

No se muestran tokens, tokens/s ni porcentaje de generación en directo: los providers actuales devuelven esas métricas al finalizar y usan `stream=false`.

La reordenación envía la lista completa de tareas pendientes. Si la cola cambia entre lectura y escritura, la UI muestra el conflicto `409`, recarga el snapshot y no aplica un orden parcial.

## Pantalla 2 — Probador de prompts

La entrada `Prompt` y la entrada `JSON` terminan en `content.prompt`. En modo JSON se valida únicamente la sintaxis y se conserva el texto exacto. El formato de entrada y `output.format` son controles independientes.

### Modelo único

La selección exacta está implementada mediante la referencia opcional `target_model`, que contiene `provider`, `deployment` y `model`. Con fallback desactivado, el router comprueba la coincidencia completa y falla si no está disponible. `preferred_model` se mantiene por compatibilidad, pero no identifica deployment ni proveedor.

### Mixture manual

- Se ofrecen `mixture_of_agents/fast` —serial— y `mixture_of_agents/slow` —paralelismo interno acotado— cuando sus respectivos backends estén implementados.
- Se seleccionan de uno a cinco proponentes y un árbitro del catálogo actual.
- Cada referencia se valida por `provider/deployment/model` antes de encolar y de nuevo antes de invocar.
- Los roles son etiquetas de trazabilidad; todos los proponentes reciben el mismo prompt opaco.
- `slow` ejecuta los proponentes en paralelo o por oleadas según el plan de recursos; el árbitro siempre se ejecuta después. `fast` permanece serial.

### Resultado e historial

- `raw` presenta exactamente el contenido persistido y escapado.
- La vista JSON solo formatea una copia para lectura cuando la respuesta es JSON válido; no modifica el resultado.
- Tokens, coste y latencia aparecen después de cada invocación terminada.
- El historial procede de SQLite, se pagina y se filtra mediante `content.metadata.origin = prompt_tester`.
- Repetir crea una nueva `idempotency_key`; reenviar accidentalmente la misma operación conserva la semántica idempotente normal.
- El timeout visible gobierna la operación completa mediante `execution.timeout_seconds`, acotado por el límite administrativo del Broker. Al vencer, se cancelan las operaciones provider y se persiste `TASK_TIMEOUT`.

`output.language` es metadata contractual, no una garantía de idioma: el Broker no reescribe el prompt. La interfaz no debe presentarlo como una instrucción que el modelo vaya a obedecer.

## Pantalla 3 — Comparación de modelos

La vista representa una única tarea `mixture_of_agents`, no varios workflows concurrentes.

- En `fast`, la línea temporal muestra la secuencia serial real.
- En `slow`, la línea temporal muestra carriles solapados únicamente cuando las invocaciones se ejecutaron realmente en paralelo, además de waves, límites y pico de VRAM observado.
- Cada propuesta muestra estado, modelo solicitado y efectivo, tokens, coste y latencia al terminar.
- El panel del árbitro muestra la síntesis raw y la lista completa de candidatos que recibió.
- No se muestra “confianza”, “relevancia”, “cobertura”, “consistencia” ni qué candidato influyó en la síntesis mientras esos valores no sean producidos por un contrato validado.
- No existe un interruptor que fuerce paralelismo inseguro: el usuario elige `slow` y puede fijar un máximo, pero el Resource Scheduler decide el plan seguro.
- No se ofrecen presets `standard`, `verified` o `high_stakes` hasta que estén implementados y probados.

## Proyecciones de lectura necesarias

Las rutas HTMX deben depender de servicios de consulta, nunca de SQL dentro de las plantillas. Antes de construir las pantallas se implementarán DTOs paginados para:

- resumen operativo por ventana temporal;
- cola y tareas con destino solicitado/efectivo;
- detalle de tarea, eventos e invocaciones;
- historial del probador;
- catálogo con provider/deployment/model/capacidades/contexto;
- recursos observados de Ollama y leases;
- salud actual e historial de cambios;
- uso agregado por periodo y proveedor.

La base de estas proyecciones está implementada en `app/dashboard.py` y documentada en [`Phase_5_Read_Models.md`](Phase_5_Read_Models.md). La pantalla de Operación usa `app/dashboard_web.py`, plantillas Jinja2 y fragmentos hipermedia servidos localmente. Permanecen pendientes el historial proactivo de salud y los timestamps de inicio/fin necesarios para medir concurrencia observada.

Los endpoints JSON públicos existentes mantienen su compatibilidad. Las proyecciones se exponen bajo `/api/v1/dashboard/*` y alimentan `/dashboard` y los fragmentos `/dashboard/fragments/*` mediante una misma capa de aplicación.

## Estado de la fase 5.2

- Implementados resumen operativo, cola, tarea activa, salud y recursos/VRAM.
- Implementados polling independiente, actualización manual, reordenación subir/bajar y cancelación.
- Las acciones reutilizan `TaskRepository`; no llaman a providers ni crean rutas de ejecución alternativas.
- La UI muestra únicamente datos persistidos o snapshots actuales con timestamp y usa `N/D` cuando no existe una medición.
- Si Ollama no permite obtener `/api/ps`, el panel sigue cargando y marca recursos como `unavailable`/`N/D`.
- Los recursos CSS y JavaScript son locales. El runtime actual implementa solo el subconjunto de atributos hipermedia utilizado por la pantalla, sin dependencia de CDN.
- Prueba de integración disponible para renderizado, fragmento de cola, reordenación, cancelación y recursos estáticos.

## Seguridad

- Todo prompt, respuesta y error se escapa como texto; no se renderiza Markdown o HTML del modelo sin sanitización explícita.
- HTMX y los recursos estáticos se sirven localmente; el dashboard no depende de CDN.
- Las acciones mutables validan `Origin`/`Host` y usan protección CSRF.
- Los prompts se ocultan en listados y solo se revelan en el detalle de una tarea.
- No se muestran headers, claves ni cadenas privadas de razonamiento.
- La gestión de claves queda fuera de la primera entrega del dashboard. Se mantiene Windows Credential Manager mediante CLI hasta disponer de autenticación administrativa y auditoría específicas.

## Prerrequisitos bloqueantes

1. **Base completada:** admisión con capacidad uno para `single/fast` y capacidad acotada para proponentes `slow`, manteniendo un solo workflow activo.
2. **Completado:** selección exacta `provider/deployment/model`, incluida la comprobación previa a cada invocación manual.
3. **Completado:** `execution.timeout_seconds` aplicado a la tarea completa con error tipado y cancelación.
4. **Base completada:** crear las proyecciones paginadas y agregaciones de resumen, tareas, detalle, recursos y uso, sin cargar todo el historial en memoria.
5. **Parcial:** exponer snapshot real de VRAM/leases. Sigue pendiente persistir cambios de salud.
6. **Completado en contrato/progreso:** diferenciar coste real, presupuesto máximo y estimación; `max_cost_usd` ya no se presenta como coste reservado.
7. Convertir los leases de modelo en contadores por invocación para impedir que una llamada descargue un modelo todavía usado por otra.
8. Reservar VRAM y peor coste de toda una wave antes de lanzarla; una reserva parcial no inicia ninguna invocación de la wave.
9. Ejecutar las waves de `slow` con `asyncio.TaskGroup`, cancelación conjunta, deadline compartido y persistencia individual de inicio/fin/error.
10. Añadir tests de autorización, CSRF, XSS, paginación, `fast` serial, solapamiento real en `slow`, límites de concurrencia, degradación a waves/secuencial, cancelación y consistencia de métricas.

## Criterio de terminación

Una pantalla solo se considera implementada cuando cada cifra y estado puede rastrearse hasta SQLite o una comprobación de infraestructura con timestamp, y cada acción tiene una prueba de integración que demuestra que usa la cola normal y respeta el slot LLM único.
