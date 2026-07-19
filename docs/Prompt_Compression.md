# Compresión de prompts del AI Broker

## Objetivo

Reducir los tokens del prompt de entrada antes de enviarlo a los LLMs, sin alterar su significado ni tocar el contenido técnico. Menos tokens de entrada significa menos coste en proveedores de pago, más margen dentro de la ventana de contexto y prompts más directos para los modelos.

El servicio es **desactivable** desde el panel de configuración del dashboard o desde `broker_config.yaml`, y los cambios aplican en caliente sin reiniciar el Broker.

## Origen de las técnicas

El diseño adapta a español las técnicas de tres proyectos:

- [caveman](https://github.com/JuliusBrussee/caveman): eliminar relleno conservando la sustancia, niveles de intensidad configurables y regla de oro de no tocar nunca código, comandos ni errores.
- [caveman-micro](https://github.com/kuba-guzik/caveman-micro): eliminar artículos, muletillas y cortesías; los términos técnicos permanecen exactos.
- [ponytail](https://github.com/DietrichGebert/ponytail): reducir tokens eliminando lo innecesario en lugar de comprimir agresivamente.

Esos proyectos actúan por instrucciones de estilo sobre la **salida** del modelo. El Broker necesita reducir la **entrada**, así que las mismas reglas se implementan como un pipeline determinista de preprocesado (`app/prompt_compressor.py`), sin ninguna llamada a modelos. El léxico está orientado a español, que es el idioma habitual de los prompts, con un subconjunto de equivalentes en inglés.

## Pipeline

1. **Protección**: los bloques de código con fence (``` o ~~~), el código inline entre backticks, las URLs y los correos se sustituyen por marcadores y se restauran intactos al final. Nunca se comprimen.
2. **Cortesías** (todos los niveles): se eliminan frases sociales enteras: "por favor", "hola", "buenos días", "muchas gracias de antemano", "si eres tan amable", "un saludo"...
3. **Relleno y envoltorios** (niveles `medium` y `aggressive`): se eliminan muletillas ("básicamente", "la verdad es que", "cabe destacar que", "en definitiva"...) y envoltorios de petición que dejan detrás un sintagma autónomo ("necesito que", "me gustaría que", "¿podrías...?").
4. **Artículos** (solo nivel `aggressive`): se eliminan artículos y determinantes (el/la/los/las/un/una/unos/unas/lo, the/a/an), al estilo caveman.
5. **Normalización**: se limpian los huérfanos de puntuación que dejan las frases eliminadas, los espacios duplicados y las líneas en blanco excesivas.

### Salvaguardas

- Si el prompt tiene menos de `min_chars` caracteres, se envía tal cual: en prompts cortos cada palabra cuenta.
- Si la compresión dejara menos del 20% del texto original (prompt patológico o léxico demasiado agresivo para ese caso), se descarta y se envía el original.
- Los límites de palabra respetan Unicode: "el" no se elimina dentro de "modelo" ni de "Manuela".

## Qué se comprime y qué no

| Flujo | ¿Se comprime? |
|-------|---------------|
| Prompt de chat en estrategia `single` | Sí |
| Prompt original en `mixture_of_agents` (proponentes y `<original_request>` del árbitro) | Sí |
| Candidatos de los proponentes dentro de la síntesis del árbitro | No |
| Embeddings (`inference_kind = embedding`) | Nunca: alterar el texto altera el vector |
| System prompts de roles | No |
| Código, URLs y correos dentro del prompt | Nunca (protegidos byte a byte) |

El punto de aplicación es `RoutedModelProvider.user_prompt` (`app/providers/routing.py`), el punto único por donde los prompts salen hacia cualquier proveedor real (Ollama, DeepSeek, OpenAI-compatible). El proveedor `bootstrap` de pruebas no comprime.

**El prompt original persiste intacto** en `content.prompt`, en la base de datos y en los artefactos (`request.md`); solo se comprime la copia que viaja al proveedor. La estimación conservadora de contexto para seleccionar modelo se hace sobre el original, por lo que la compresión nunca relaja el preflight. Cuando la compresión altera el prompt, se persiste además un evento `prompt.compressed` en la tarea (texto comprimido y tamaños), exento de la poda de eventos: el detalle de la tarea en el dashboard muestra siempre el original y el comprimido que viajó.

## Override por tarea

Cada tarea puede fijar su propia compresión con el campo opcional `prompt_compression` del contrato (`POST /api/v1/tasks`): `"off"` envía el prompt tal cual aunque el servicio global esté activo, y `"light"`/`"medium"`/`"aggressive"` sustituyen al nivel global solo para esa tarea. Ausente = configuración global. `min_chars` es siempre el global: el override cambia cuánto se comprime, no la regla de prompts cortos. El probador del dashboard expone esta elección en el selector "Compresión del prompt para esta prueba", y su vista previa replica exactamente lo que hará el router. El flag `prompt_compression_override: true` de `GET /api/v1/capabilities` anuncia el soporte.

**Interacción con ficheros adjuntos (fase 7):** cuando una tarea lleva adjuntos `broker_file`, la expansión del prompt fija la compresión a `off` salvo que la tarea traiga un override explícito. Motivo: el Markdown de un documento contiene tablas, cifras y código que la poda léxica corrompería. El prompt original del cliente se conserva en `request_json` en cualquier caso.

## Configuración

En `broker_config.yaml`:

```yaml
prompt_compression:
  enabled: true        # false desactiva el servicio por completo
  level: medium        # light | medium | aggressive
  min_chars: 40        # prompts más cortos se envían sin comprimir
```

| Nivel | Reglas aplicadas |
|-------|------------------|
| `light` | Cortesías y aperturas sociales |
| `medium` (por defecto) | + muletillas, relleno y envoltorios de petición |
| `aggressive` | + artículos y determinantes (estilo caveman) |

Desde el dashboard: panel **Configuración → Compresion de prompts**, con checkbox de activación, selector de nivel y mínimo de caracteres. Como el resto del panel, admite "Revisar sin guardar" y los cambios guardados se aplican en memoria al instante (`reload_config` reconstruye el compresor).

## Observabilidad

Cada compresión efectiva emite un log estructurado:

```
event: prompt.compressed
level: medium
chars_before: 271
chars_after: 191
ratio: 0.7
```

No se registra el contenido del prompt, en línea con la política de logging del Broker.

## Límites actuales

- La compresión es por reglas léxicas; no hay compresión semántica (tipo LLMLingua) ni reescritura por modelo.
- El léxico cubre español y un subconjunto de inglés; otros idiomas pasan casi intactos (solo se normaliza el espaciado).
- La reducción se mide en caracteres, no en tokens del tokenizer real de cada proveedor.
- No hay métrica agregada de ahorro en el dashboard; el ahorro se observa por log y por `tokens_input` de las invocaciones.

## Tests

`tests/test_prompt_compressor.py` cubre: desactivación, umbral `min_chars`, eliminación de cortesías/muletillas por nivel, preservación de código/URLs/correos, límites de palabra Unicode, salvaguarda de sobrecompresión, validación de nivel, defaults de configuración y recarga en caliente en `RoutedModelProvider`.
