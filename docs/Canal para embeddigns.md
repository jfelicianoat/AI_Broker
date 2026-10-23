Encargo: embeddings en lote y carril propio para embeddings

Contexto

Knowledge Orchestrator va a indexar cada nota en fragmentos con embeddings: unos 1 800 fragmentos hoy y decenas de miles en el futuro. También calculará un embedding por cada pregunta de los usuarios. Medido el 21-oct con bge-m3:latest en Ollama:
- La invocación tarda 217 ms (latency_ms de la invocación).
- La tarea entera tarda 35–54 s con priority: 1. Se queda en queued hasta que termina la inferencia de chat que esté corriendo, porque max_active_workflows = 1.

Con una tarea por texto, indexar lo actual llevaría unas 17 h con el Broker libre. Hacen falta dos cosas, y las dos son cambios aditivos al contrato.

1. Lote de textos en una tarea de embedding

- Campo nuevo content.inputs: [string], solo con inference_kind: "embedding". No puede ir junto con content.prompt: si llegan los dos, 422.
- Máximo de textos por lote anunciado en capabilities, por ejemplo embedding_batch_max: 64.
- Resultado nuevo result.embeddings: [[float]], en el mismo orden que inputs, junto con el modelo usado y sus dimensions. result.embedding sigue igual para la petición de un solo texto.
- Un lote, un modelo: no se sustituye el modelo a mitad del lote. Si no está disponible, falla el lote entero.
- Nada de truncar en silencio: si un texto no cabe en el contexto del modelo, la tarea falla con un código propio (por ejemplo EMBEDDING_INPUT_TOO_LONG) e indica el índice del texto. Un vector de un texto recortado parece válido y no lo es.
- El artefacto final (embedding_output, final: true) lleva todos los vectores.

2. Carril de embeddings que no ocupe el turno de inferencia

- Las tareas inference_kind: "embedding" corren en un carril propio y no consumen el único workflow de inferencia. Es lo mismo que ya hace waiting_for_dependencies.
- La concurrencia del carril se configura (por defecto 1) y se anuncia en capabilities. Por ejemplo, work_lanes incluiría "embedding", más embedding_lane: {max_parallel, batch_max}.
- Dentro del carril se respeta priority: el embedding de una pregunta (prioridad 1) pasa delante del indexado masivo (prioridad 100).
- Memoria: el planificador de memoria tiene que contar el modelo de embeddings (bge-m3 son 567 M de parámetros en F16). Si cargarlo obligara a descargar el modelo de chat en curso, la tarea espera en waiting_for_memory; nunca expulsa el modelo de la inferencia que está corriendo.

3. Un arreglo de paso

Hoy un embedding exige output.format: "json", y a la vez el validador exige output.json_schema cuando el formato es json. El resultado es que la única forma de enviarlo es con un esquema que no se usa para nada ({"type": "object"}). Para inference_kind: "embedding", el esquema no debería ser obligatorio.

4. Identidad del modelo

Junto a los vectores tienen que venir provider, model, dimensions y el digest (ya está en execution_fingerprint). Con eso KO detecta que el modelo ha cambiado y recalcula el índice. Mezclar vectores de dos modelos daría resultados basura sin ningún error visible.

Pruebas de aceptación

1. Un lote de 64 textos devuelve 64 vectores en orden, y cada uno coincide (coseno ≈ 1) con el que sale enviando ese texto solo.
2. Con una inferencia de chat larga en marcha, una tarea de embedding empieza en menos de 1 s y termina sin esperar al chat.
3. Con el carril lleno de indexado a prioridad 100, una tarea a prioridad 1 es la siguiente en ejecutarse.
4. Un texto demasiado largo hace fallar el lote con el código propio y el índice del texto, sin dar ningún vector.
5. content.inputs junto con content.prompt → 422. Con más textos que embedding_batch_max → 422.
6. capabilities anuncia el lote y el carril. Un Broker anterior no los anuncia, y así el cliente sabe que no debe usarlos.
7. Rendimiento: 1 800 fragmentos de unos 1 500 caracteres con bge-m3 local, en lotes de 64, en menos de 10 min.

Documentación

Actualizar Client_API.md (§ de inference_kind, resultados y capabilities) y subir la versión menor del contrato. KO comprobará esa versión y las capabilities antes de enviar lotes.