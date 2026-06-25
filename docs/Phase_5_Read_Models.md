# Fase 5.1 — Read models operativos

## Estado

Implementación base completada el 24 de junio de 2026. Las proyecciones son de solo lectura y están separadas del repositorio transaccional que crea, reclama y actualiza tareas.

## Endpoints

| Endpoint | Fuente | Uso |
|---|---|---|
| `GET /api/v1/dashboard/summary` | `tasks` y `model_invocations` | Cola actual y métricas de una ventana temporal |
| `GET /api/v1/dashboard/tasks` | `tasks` + agregados de invocaciones | Listado paginado y filtros por estado/origen |
| `GET /api/v1/dashboard/tasks/{task_id}` | tarea, request, invocaciones y eventos | Diagnóstico completo de una tarea |
| `GET /api/v1/dashboard/resources` | Ollama `/api/ps`, leases y configuración | VRAM observada, reservas y modelos cargados |
| `GET /api/v1/usage` | `model_invocations` | Uso mensual real por provider |
| `GET /api/v1/capabilities` | configuración y contrato | Negociación de presets y límites |

## Semántica del resumen

- `queued` y `active` representan el estado actual, no una ventana histórica.
- `completed`, `failed` y `cancelled` usan `updated_at` dentro de `window_hours`.
- `success_rate = completed / (completed + failed)`; las cancelaciones no se consideran fallos del modelo.
- p50/p95 se calculan exclusivamente con `model_invocations.status = completed` y latencia persistida.
- coste y tokens son sumas reales de invocaciones terminadas; no incluyen presupuesto máximo ni estimaciones.
- `oldest_queued_seconds` se calcula desde el timestamp durable de la tarea más antigua pendiente.

## Listado y detalle

El listado no incluye prompts. Expone únicamente identidad, estado, origen, estrategia, preset, destino solicitado/efectivo y agregados técnicos. Admite:

- `page >= 1`;
- `1 <= page_size <= 200`;
- filtro exacto por `TaskStatus`;
- filtro exacto por `content.metadata.origin`.

El detalle sí devuelve el request persistido completo, resultado/error, invocaciones y hasta los 500 eventos más recientes. La futura UI debe ocultar el prompt por defecto y revelar el contenido solo en la vista de detalle.

## Recursos

Para Ollama, el snapshot consulta `/api/ps` en el momento de la petición y combina:

- VRAM observada de modelos cargados;
- contexto declarado por el runtime;
- contador de leases internos;
- bytes reservados por el lifecycle manager;
- presupuesto, margen de seguridad y paralelismo configurado.

El provider `bootstrap` devuelve un snapshot vacío y solo se usa en pruebas. Si Ollama no responde, el endpoint conserva el error tipado del provider; no inventa un valor cero saludable.

## Límites pendientes

- El historial proactivo de salud todavía no se persiste; `/health` proporciona el estado actual.
- Las invocaciones nuevas persisten `started_at/completed_at` calculados desde la latencia medida por el provider al finalizar la llamada. Filas antiguas sin esos campos no se usan para demostrar solapamiento real.
- El panel HTML de la fase 5.2 consume estas proyecciones mediante `app/dashboard_web.py`; las plantillas no ejecutan SQL.
- Los fragmentos de resumen, cola, tarea activa, salud y recursos se actualizan independientemente sin bloquearse por el workflow LLM activo.
