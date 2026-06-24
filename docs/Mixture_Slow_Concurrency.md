# `mixture_of_agents/slow` — contrato de concurrencia

## Estado

Base funcional implementada el 24 de junio de 2026: `ExecutionPreset` acepta `slow`, el scheduler crea `parallel/waves/sequential`, el coordinador lanza concurrentemente los proponentes de una wave y `fast` permanece serial. La activación productiva sigue condicionada a persistencia de timestamps de inicio, negociación de capacidades y smoke tests con providers reales/VRAM.

## Semántica

`slow` prioriza una comparación más amplia y auditable. El nombre describe el perfil de mayor trabajo, no obliga a que cada invocación sea secuencial.

1. El Broker reclama una única tarea y mantiene `max_active_workflows = 1`.
2. Selecciona y valida todos los proponentes y el árbitro.
3. Construye un plan `parallel`, `waves` o `sequential` antes de invocar.
4. Reserva atómicamente VRAM, coste y permisos de provider para la primera wave.
5. Lanza concurrentemente los proponentes de esa wave.
6. Persiste resultado o error por invocación y libera su reserva.
7. Repite las waves necesarias.
8. Comprueba quórum y ejecuta un único árbitro después de la barrera.

Nunca se solapan invocaciones de dos tareas Broker diferentes.

## Política de scheduling

| Petición | Comportamiento |
|---|---|
| `adaptive` | Usa el máximo paralelismo seguro y degrada a waves o serial |
| `parallel` | Exige que todos los proponentes quepan en una wave; si no, falla antes de invocar |
| `waves` | Usa waves acotadas por recursos; una wave puede contener una sola invocación |
| `sequential` | Ejecuta uno a uno aunque el preset sea `slow` |

El error de un `parallel` imposible será `PARALLEL_CAPACITY_INSUFFICIENT`. No se afirmará que hubo paralelismo si el provider serializó internamente las peticiones; el resultado distinguirá plan solicitado, plan lanzado y concurrencia observada.

## Admisión y recursos

- `max_parallel_invocations` limita la concurrencia total del workflow.
- Cada provider puede imponer un límite inferior por cuota o capacidad.
- Para modelos locales se reservan pesos únicos, KV cache por invocación, buffers y margen de seguridad.
- La reserva de una wave es todo-o-nada. No se inicia una parte mientras la planificación de la misma wave sigue abierta.
- Dos invocaciones del mismo modelo comparten pesos, pero consumen contextos separados.
- Los leases se contabilizan por invocación. Un modelo solo puede descargarse cuando su contador llega a cero.
- El peor coste autorizado para todas las invocaciones de una wave se reserva antes de lanzarla. El coste real sustituye progresivamente la reserva.

## Ejecución asíncrona

- El coordinador usa `asyncio.TaskGroup` por wave.
- Todas las invocaciones comparten un deadline monotónico derivado de `execution.timeout_seconds`.
- Cancelar la tarea cancela el grupo, espera la finalización de los `finally`, libera reservas y confirma la descarga segura.
- Un fallo retryable individual no cancela automáticamente las demás invocaciones; al cerrar la wave se evalúan reintentos y quórum.
- Un fallo de privacidad, presupuesto o contrato cancela la wave completa.
- Los resultados se ordenan por ordinal de proponente, no por orden de finalización.

## Persistencia y observabilidad

Cada invocación se inserta con estado `running` antes de llamar al provider y termina como `completed`, `failed` o `cancelled`. Debe conservar:

- `started_at` y `completed_at` con precisión suficiente para comprobar solapamiento;
- provider, deployment, modelo, rol y ordinal;
- wave, intento y límite de concurrencia aplicado;
- tokens, coste, latencia y error tipado;
- reserva y uso observado de VRAM cuando estén disponibles.

El resultado de tarea incluye actualmente `scheduling.mode_used`, waves y `max_parallel_invocations_launched`. Antes de dibujar carriles, se añadirán `scheduling.requested` y timestamps por invocación para calcular el máximo observado; lanzar coroutines simultáneamente no demuestra que un runtime externo las ejecutase a la vez.

## Cambios contractuales

- `slow` ya está añadido a `ExecutionPreset` en el Broker; falta publicarlo en la revisión compartida del contrato.
- Mantener `fast` como valor predeterminado.
- Rechazar `single + slow`.
- Permitir `slow` solo para `inference_kind = chat` y `strategy = mixture_of_agents`.
- Actualizar el validador compartido del Knowledge Orchestrator antes de permitir que lo envíe; hasta entonces solo el Probador del Broker podrá habilitarlo tras validación local.
- Negociación implementada en `GET /api/v1/capabilities`: publica versión de contrato, presets, modos de scheduling y límites para impedir que un cliente envíe `slow` a un Broker incompatible.

## Criterios de aceptación

1. Dos proponentes simulados muestran intervalos solapados con `slow + parallel`.
2. La misma selección con `fast` no muestra solapamiento.
3. Nunca existen dos tareas activas, aunque entren nuevas peticiones durante una wave.
4. La concurrencia observada nunca supera el límite global ni el del provider.
5. VRAM insuficiente produce waves en `adaptive` y error previo en `parallel`.
6. La cancelación de una wave no deja tareas HTTP, leases ni reservas huérfanas.
7. Un modelo compartido no se descarga hasta terminar su última invocación.
8. El coste reservado no puede superarse por una carrera entre proponentes.
9. Reiniciar recupera la tarea sin presentar invocaciones `running` antiguas como activas.
10. Un smoke test con providers reales confirma concurrencia lanzada y registra si el runtime la ejecutó o la serializó internamente.
