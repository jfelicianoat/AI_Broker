# PROMPT DE IMPLEMENTACIÓN — AI_Broker
## Capa System-1 centralizada, LAYA vía MCP, proveedor Ollama alternativo y routing semántico de modelos

## ROL

Actúa como arquitecto y programador senior responsable de **AI_Broker**. Debes implementar una capa System-1 reutilizable sin romper el comportamiento actual del Broker. Trabaja sobre el repositorio real: no diseñes una arquitectura paralela imaginaria.

## OBJETIVO REAL

Convertir AI_Broker en el único punto de acceso de las apps del TFM a modelos System-1, usando:

- **LAYA vía MCP** como proveedor principal inicial.
- Un **modelo System-1 de Ollama** como proveedor alternativo/fallback.

Además, utilizar esa capa para implementar **selección semántica del modelo**: cuando varias rutas son técnicamente válidas, determinar qué nivel de modelo necesita realmente una petición por su dificultad semántica, evitando mandar por defecto todas las tareas a modelos grandes/caros.

## ARQUITECTURA TRANSVERSAL OBLIGATORIA

1. Las aplicaciones del TFM no llamarán directamente a LAYA ni al modelo System-1 de Ollama.
2. Todas las decisiones System-1 se solicitarán a través de AI_Broker.
3. LAYA es el proveedor principal inicial y se consume mediante MCP.
4. El modelo System-1 de Ollama debe quedar soportado como proveedor alternativo/fallback a través del Broker.
5. La aplicación solicitante conserva la lógica de negocio; AI_Broker normaliza acceso y devuelve un juicio estructurado.
6. Antes de usar System-1 deben ejecutarse las reglas deterministas aplicables.
7. Si System-1 falla, no responde, supera timeout, devuelve salida inválida o confianza insuficiente, la aplicación debe volver a su comportamiento actual.
8. Todo debe poder activarse/desactivarse con configuración o feature flags.
9. No introducir servicios paralelos, bases de datos nuevas ni buses nuevos si el repositorio ya tiene una forma equivalente.

## POLÍTICA INICIAL DE CONFIANZA

Los umbrales deben ser configurables por caso de uso. No los disperses como constantes por el código.

Valores iniciales orientativos:

- `0.97`: autoaceptación muy conservadora para decisiones cuyo falso positivo pueda cortar trabajo necesario o saltarse revisión.
- `0.90`: confianza alta para routing semántico cuando existe fallback seguro.
- `0.85`: confianza suficiente para ranking/selección no destructiva.
- `0.70`: por debajo debe considerarse dudoso y activarse fallback o ruta conservadora.
- En multiclase, usar además margen mínimo top1-top2 configurable; inicio recomendado `0.15`.

Si el modelo no devuelve probabilidades calibradas, el campo debe tratarse como `confidence_score`, no como probabilidad real.

## CALIBRACIÓN OBLIGATORIA

Antes de declarar estable un caso de uso:

1. Crear un conjunto de validación con ejemplos reales o representativos.
2. Guardar decisión esperada.
3. Ejecutar LAYA y, cuando proceda, el System-1 de Ollama.
4. Medir al menos accuracy, precision/recall/F1 por clase, falsos positivos/negativos, estabilidad entre ejecuciones, latencia p50/p95, porcentaje de fallback, ahorro de tokens/tiempo/llamadas y Brier/ECE solo si se afirma que la salida es probabilidad calibrada.
5. Elegir umbrales según el coste del error de cada caso de uso.
6. Documentar valores y mantenerlos configurables sin recompilar.

## CONTRATO DE DECISIÓN ESPERADO

No obligues a crear exactamente estas clases si el repositorio ya tiene equivalentes, pero el comportamiento observable debe ser equivalente a:

```json
{
  "use_case": "identificador_del_caso",
  "decision": "valor_elegido",
  "confidence": 0.93,
  "confidence_is_calibrated": false,
  "alternatives": [
    {"value": "opcion_2", "confidence": 0.12}
  ],
  "provider": "laya_mcp",
  "model": "nombre_real_si_se_conoce",
  "latency_ms": 123,
  "fallback_used": false,
  "reason_code": null
}
```

Requisitos:

- salida estructurada y validable;
- no depender de texto libre para ejecutar lógica;
- conservar proveedor y latencia para observabilidad;
- diferenciar confianza calibrada de simple score;
- registrar motivo de fallback;
- no guardar prompts sensibles completos en logs salvo que el proyecto ya lo contemple y esté permitido.

## ALCANCE FUNCIONAL

### A. Abstracción System-1

Localiza primero la arquitectura actual de providers/adapters/model clients del Broker.

Integra System-1 en esa arquitectura existente. Debe soportar como mínimo:

- juicio binario;
- elección entre varias opciones;
- score/valor ordinal cuando el proveedor lo permita;
- confidence/score normalizado cuando exista;
- timeout;
- cancelación si la infraestructura actual la soporta;
- fallback de proveedor;
- validación estricta de salida;
- telemetría.

No crees un microservicio separado si AI_Broker ya puede alojar esta capacidad.

### B. Proveedor LAYA vía MCP

Implementa o reutiliza un adapter que:

- use el mecanismo MCP ya existente si lo hay;
- no replique un cliente MCP alternativo;
- traduzca formatos de LAYA al contrato interno de juicio;
- trate cualquier salida no interpretable como fallo;
- registre proveedor, latencia y causa de error;
- no convierta texto libre ambiguo en una decisión silenciosamente.

### C. Proveedor System-1 de Ollama

Inspecciona si AI_Broker ya tiene integración con Ollama.

- Si existe, extiéndela mínimamente para soportar el modelo System-1.
- Si no existe, añade el adapter más pequeño compatible con el patrón de providers actual.
- No hardcodees el nombre del modelo.
- El orden inicial de providers debe poder configurarse, por ejemplo: `laya_mcp -> ollama_system1`.

### D. API/contrato de juicio para otras apps

Las apps deben poder solicitar algo equivalente a:

```json
{
  "use_case": "goal_completion",
  "input": {"...": "..."},
  "decision_type": "binary|choice|score",
  "options": ["..."],
  "threshold_profile": "goal_completion"
}
```

No impongas necesariamente un endpoint HTTP nuevo si el Broker usa otro transporte. Usa el patrón dominante del repositorio.

La respuesta deberá permitir saber:

- decisión;
- confidence/score;
- si está calibrado;
- alternativas cuando proceda;
- provider;
- modelo;
- latencia;
- si hubo fallback;
- motivo de error/fallback.

### E. Selección semántica de modelo

Secuencia obligatoria:

1. Aplicar restricciones deterministas primero: capacidades obligatorias, contexto mínimo, visión, provider disponible, privacidad, límites de coste configurados e incompatibilidades conocidas.
2. Crear el conjunto de modelos/rutas técnicamente válidos.
3. Solo si quedan varias opciones razonables, pedir a System-1 que clasifique la tarea por dificultad o nivel de capacidad requerido.
4. Elegir entre rutas ya válidas.
5. Si la confianza es insuficiente, usar el router actual o una ruta conservadora; nunca degradar a un modelo claramente insuficiente.

No uses System-1 para decidir cosas que los metadatos del modelo resuelven de forma exacta.

### F. Umbrales iniciales para routing

Configurar, no hardcodear:

- confianza mínima routing semántico: `0.90`;
- margen mínimo top1-top2: `0.15`;
- por debajo o con margen insuficiente: routing actual/fallback conservador;
- permitir experimentar después con `0.85` si benchmark demuestra que mantiene calidad.

Optimiza **precision del down-routing**: es preferible enviar una tarea simple a un modelo grande que una compleja a uno insuficiente.

## OBSERVABILIDAD

Añade métricas compatibles con la infraestructura actual:

- llamadas System-1 por use case;
- provider utilizado;
- latencia;
- errores;
- fallbacks LAYA -> Ollama;
- fallbacks System-1 -> routing actual;
- distribución de confidence;
- decisión de routing;
- modelo finalmente elegido;
- comparación opcional en shadow mode con router actual.

Implementa shadow mode si encaja razonablemente:

- System-1 calcula decisión;
- no modifica routing real;
- se registra qué habría elegido;
- permite recopilar benchmark sin riesgo.

## CONFIGURACIÓN

Centraliza las claves siguiendo el sistema existente. Debe poder configurarse como mínimo:

- enable/disable System-1;
- provider priority;
- timeout;
- nombre/configuración del modelo Ollama;
- configuración MCP de LAYA si no está centralizada;
- thresholds por use case;
- margen top1-top2;
- shadow mode;
- fallback policy.

## TESTS OBLIGATORIOS

1. LAYA responde correctamente.
2. LAYA falla -> Ollama responde.
3. Ambos fallan -> ruta previa del Broker.
4. Salida inválida -> no se ejecuta decisión corrupta.
5. Baja confianza -> router previo.
6. Margen top1-top2 insuficiente -> router previo.
7. Restricción determinista invalida modelo -> System-1 no puede reintroducirlo.
8. Feature flag desactivada -> comportamiento previo.
9. Telemetría incluye provider, latencia y fallback.
10. Router semántico con tareas simples, medias y complejas.

## CRITERIOS DE ACEPTACIÓN

Se considera terminado solo si:

- ninguna app necesita llamar directamente a LAYA/Ollama;
- LAYA funciona a través del Broker por MCP;
- Ollama puede actuar como alternativa configurable;
- el routing previo sigue funcionando;
- la respuesta de juicio es estructurada;
- thresholds configurables;
- fallback seguro;
- tests y evidencias;
- respeto por arquitectura existente.

## PROCESO DE TRABAJO OBLIGATORIO

Antes de modificar código:

1. Inspecciona el repositorio completo relevante.
2. Identifica módulos existentes, interfaces públicas, configuración, tests, telemetría, clients/providers y fallbacks.
3. Escribe resumen breve de arquitectura encontrada y plan basado en código real.
4. Reutiliza patrones existentes. No inventes una segunda arquitectura.
5. Si el código contradice una suposición, prioriza preservar comportamiento/contratos y hacer el cambio mínimo necesario.
6. Implementa en incrementos pequeños.
7. Ejecuta tests existentes tras cada bloque relevante.
8. Añade tests nuevos para normal, límites, fallos, baja confianza y feature off.
9. No declares nada como verificado sin haberlo comprobado.

## RESTRICCIONES

- No refactorices módulos ajenos sin necesidad demostrable.
- No cambies contratos públicos sin necesidad.
- No introduzcas nueva base de datos.
- No hardcodees modelos, URLs, puertos, thresholds o timeouts.
- No elimines el flujo previo.
- No introduzcas llamadas directas a LAYA/Ollama desde otras apps.

## SALIDA FINAL DEL PROGRAMADOR

Entrega:

1. Diagnóstico de arquitectura encontrada.
2. Plan ejecutado.
3. Cambios implementados.
4. Archivos modificados.
5. Configuración añadida.
6. Tests y resultado.
7. Ejemplo real de petición System-1 y respuesta normalizada.
8. Ejemplo de routing semántico.
9. Métricas/benchmark disponibles.
10. Pendientes reales, sin inventar.
