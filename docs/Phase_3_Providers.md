# Fase 3 — Providers y enrutamiento

## Estado

Completada el 2026-06-23.

## Flujo operativo

1. El router obtiene el catálogo habilitado de Ollama y, opcionalmente, DeepSeek.
2. Filtra por `allowed_providers`, prioriza `preferred_model` y respeta `fallback_allowed`.
3. Un semáforo global garantiza una sola llamada LLM simultánea en todo el Broker.
4. Para Ollama, el lifecycle manager comprueba `/api/ps`, reserva capacidad y evita descargar modelos con lease.
5. La llamada usa `keep_alive: -1`; el bloque `finally` envía `keep_alive: 0` y confirma la descarga antes de liberar el slot.
6. La respuesta normaliza contenido, tokens, coste y latencia. Los errores se persisten con código y `retryable`.

## Catálogo

`GET /api/v1/models` se alimenta de:

- Ollama `/api/tags`: nombre, familia, tamaño, parámetros y cuantización.
- Ollama `/api/show`: capacidades y ventana de contexto.
- DeepSeek `/models`: modelos accesibles para la credencial configurada.

No existen listas de modelos hardcodeadas. Si Ollama o DeepSeek no responde, su health check queda `unavailable`; SQLite sigue determinando readiness para que la API pueda aceptar trabajo en cola durante una caída temporal del proveedor.

## Seguridad y coste

- DeepSeek está deshabilitado por defecto.
- La clave se resuelve desde `DEEPSEEK_API_KEY` o `keyring`; no se persiste en configuración, SQLite, artefactos ni logs.
- `local_only` conserva únicamente modelos locales de Ollama; las etiquetas con `remote_host` quedan clasificadas como deployment `cloud` y se excluyen.
- DeepSeek estima el coste máximo antes de enviar la petición y el coordinador comprueba también el coste acumulado real.
- Las tarifas son configuración operativa: deben actualizarse antes de habilitar DeepSeek.

## Errores tipados

Los códigos principales son `PROVIDER_UNAVAILABLE`, `MODEL_UNAVAILABLE`, `MODEL_ERROR`, `INVALID_PROVIDER_RESPONSE`, `CREDENTIALS_UNAVAILABLE`, `BUDGET_EXCEEDED`, `VRAM_INSUFFICIENT`, `MODEL_UNLOAD_FAILED` y `TASK_CANCELLED`.

## Verificación

- Ocho pruebas unitarias cubren discovery, contexto/capacidades, inferencia, descarga, cancelación, credenciales, coste, routing, fallback, aislamiento local/cloud y serialización global.
- Las cinco pruebas de contrato continúan pasando.
- Se verificó una inferencia real con `granite4.1:3b`: 16 tokens de entrada, 2 de salida, respuesta `OK` y descarga confirmada mediante `/api/ps`.
