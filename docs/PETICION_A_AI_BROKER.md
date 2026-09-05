# Petición de contrato a AI_Broker — desde Agora (2026-09-03)

Mensaje para quien mantiene AI_Broker. Agora es un cliente nuevo: convierte trabajo
autónomo en tarjetas (CARDs) que se ejecutan sin nadie delante, así que necesita poder
**demostrar por contrato** lo que ocurrió, no deducirlo.

**Todo lo que sigue está verificado contra el broker en marcha en
`http://192.168.1.52:8765` (contrato 2.9) los días 3 de septiembre de 2026.** No se ha
usado el código fuente como evidencia: la copia disponible en el PC principal está
desactualizada. Cada punto incluye cómo reproducirlo con `curl`/HTTP.

Antes de las peticiones, lo que **ya funciona bien y no hay que tocar**: la idempotencia
(misma clave + mismo cuerpo → `200` y el mismo `task_id`; misma clave + cuerpo distinto →
`409 IDEMPOTENCY_CONFLICT`), los artefactos con `sha256` y `media_type`, la contabilidad
contractual de `result.usage`, el `execution_fingerprint`, y el eco de determinismo
(`seed_status` / `top_p_status`). Eso resuelve la mayor parte de lo que Agora necesitaba.

Quedan cinco cosas.

---

## 1. `role` y `status` de las invocaciones son texto libre: no se puede saber qué trabajo es mío

**Qué pasa.** `GET /api/v1/tasks/{id}/invocations` mezcla, bajo el mismo `task_id`, las
invocaciones que ejecutan la tarea del cliente con las que el broker lanza por su cuenta.
En el OpenAPI del broker en marcha, `TaskInvocationItem.role` y `.status` están declarados
como `{"type": "string"}`, sin `enum`.

Sobre 25 tareas reales aparecen tres roles: `single`, `shadow_probe` y `confidence_judge`.
Los tres llegan a `completed` y los tres reportan `generation`.

**Por qué me duele.** Agora tiene que verificar que la ejecución cumplió la política de la
tarjeta (modelo exacto, `temperature`, `seed`). Al no poder distinguir los roles, validaba
la política contra invocaciones que no eran la tarjeta, y facturaba a la tarjeta lo que el
broker exploraba por su cuenta. Hoy lo resuelvo con una **lista negra de nombres de rol
escrita a mano** (`{"shadow_probe", "confidence_judge"}`), que se rompe en cuanto añadáis
un cuarto rol.

**Qué pido.**

1. Enumerar `role` y `status` en el OpenAPI (`enum`), para que un cliente pueda validarlos.
2. Añadir a cada invocación un booleano explícito, p. ej. `contractual: true|false`, que
   diga si esa invocación forma parte del trabajo que pedí o es trabajo propio del broker.
   Con eso el cliente deja de adivinar por el nombre.

**Reproducir.**

```bash
curl -s -H "X-Admin-Token: $TOKEN" \
     http://192.168.1.52:8765/api/v1/tasks/task_712487dc8d0e4975aaa634fa7f0d49fa/invocations
```

---

## 2. Una segunda invocación procesa mi contenido en un modelo que yo no autoricé

Este es el punto importante, y el único que puede requerir una decisión de diseño.

**Qué pasa.** Envié una tarea con determinismo estricto:

```json
"model_requirements": {
  "target_model": {"provider": "lmstudio", "deployment": "local", "model": "laguna-xs-2.1"},
  "allowed_providers": ["lmstudio"],
  "fallback_allowed": false
}
```

El broker la sirvió correctamente con `laguna-xs-2.1`. Pero bajo el **mismo `task_id`**
lanzó además una invocación `shadow_probe` en `huihui-qwen3.8-27b-abliterated`, otro
modelo, que también terminó `completed`:

| rol | modelo | tokens entrada | tokens salida |
|---|---|---:|---:|
| `single` | `laguna-xs-2.1` | 341 | 1045 |
| `shadow_probe` | `huihui-qwen3.8-27b-abliterated` | 354 | 3980 |

Los tokens de entrada casi coinciden (341 / 354, diferencia compatible con otro
tokenizador), así que **el sondeo está procesando el mismo contenido que la tarea**. Y
genera 3980 tokens de salida: no es una medición barata, es inferencia real que ocupa la
máquina.

**Por qué me duele.** Un worker atómico ejecuta un contrato: PROFILE + SKILL + CARD. Si la
tarjeta declara `fallback_allowed: false` y un modelo exacto, es porque alguien decidió que
**solo ese modelo** debe ver ese contenido. Hoy no puedo prometérselo a quien firma la
tarjeta, y con `data_classification: confidential` o `local_only` eso pasa de incómodo a
inaceptable.

**Qué pido**, por orden de preferencia:

1. **Un opt-out por tarea.** Un campo tipo `auxiliary_invocations: false` (o que
   `exclude_from_model_learning: true` ya lo implique, si os encaja) que garantice que solo
   el modelo aprobado ve el contenido. Es lo que necesito para tarjetas estrictas y para
   clasificaciones sensibles.
2. **Documentar la garantía real**: ¿el sondeo respeta `risk.data_classification`,
   `model_requirements.allowed_providers` y `cloud_allowed`? En mis pruebas se quedó dentro
   del `provider` permitido y en modelos locales, pero **no** respetó el `target_model`
   exacto. Necesito que eso esté escrito, no observado.
3. Si el sondeo va a seguir existiendo siempre, decidlo en `GET /api/v1/capabilities` para
   que el cliente pueda rechazar de antemano las tarjetas que no lo toleran.

No estoy pidiendo que quitéis el sondeo: entiendo que el enrutado adaptativo lo necesita
para aprender. Pido poder apagarlo cuando el contrato de la tarjeta no lo permite.

---

## 3. No hay forma de verificar que `prompt_compression: "off"` se respetó

**Qué pasa.** Envío `"prompt_compression": "off"` y el broker acepta la tarea, pero el
valor efectivo **no aparece en ninguna respuesta**: ni en `GET /api/v1/tasks/{id}`, ni en
`execution_summary`, ni en `progress`, ni en la invocación.

**Por qué me duele.** Para un worker atómico, PROFILE + SKILL + CARD son el contrato: si se
comprimen, se pueden perder garantías o instrucciones. La especificación de Agora exige una
prueba automática de que la compresión estuvo apagada, y hoy esa prueba no se puede
escribir: solo puedo comprobar que lo *pedí*, no que se *cumplió*.

**Qué pido.** Devolver el valor efectivo de `prompt_compression` en `execution_summary`
(o en la configuración efectiva de la invocación, junto a `temperature` y `seed`, que ya
hacéis muy bien con `seed_status`/`top_p_status`). Con un eco del estilo
`prompt_compression: {"requested": "off", "effective": "off"}` me sobra.

**Reproducir.** Lanzar cualquier tarea con `"prompt_compression": "off"` y buscar la cadena
en el estado de la tarea: no aparece.

---

## 4. `result` no tiene esquema; `artifacts` sí. ¿Cuál es el canónico?

**Qué pasa.** En el OpenAPI del broker en marcha, `TaskStateResponse.result` es
`{"anyOf": [{"type": "object", "additionalProperties": true}, {"type": "null"}]}`. Es decir:
un cliente que solo lea el contrato **no puede saber dónde está la respuesta**. Hay que
saber de antemano que el texto viene en `assistant_content` / `result_markdown`.

Esto ya me costó un fallo real: mi cliente buscaba la respuesta en claves que no existen y
acababa guardando el sobre JSON entero como si fuera el entregable.

Al mismo tiempo, `GET /api/v1/tasks/{id}/artifacts` **sí** devuelve algo tipado y
verificable (`artifact_id`, `filename: final.md`, `media_type`, `size_bytes`, `sha256`),
que es exactamente lo que necesita un sistema de tarjetas para cerrar con trazabilidad.

**Qué pido.** Poco, y elegid una:

- decir en la documentación del contrato que **`/artifacts` es la vía canónica** para
  obtener el entregable y que `result.assistant_content` es una comodidad; o
- tipar `result` en el OpenAPI (aunque sea un `TaskResult` con los campos garantizados) y
  declarar qué claves son estables bajo `contract_version`.

Con la primera me vale: voy a migrar Agora a `/artifacts` de todos modos.

---

## 5. Pregunta abierta: ¿cómo obtiene un proceso local la credencial de administración?

**Aquí no afirmo nada**, porque no es observable desde fuera y el código que tengo está
desactualizado. Necesito que me lo confirméis vosotros.

**El contexto.** Agora tiene dos piezas en el PC de IA, junto al broker: un runner y una
pasarela. Ambos hablan con el broker por loopback y necesitan el `X-Admin-Token`. El
requisito de Agora es estricto: **ese token no puede cruzar la red ni llegar nunca al PC
principal**, así que tiene que obtenerlo localmente, en la propia máquina, después de cada
arranque del broker.

**Las preguntas concretas.**

1. ¿De dónde sale hoy el `X-Admin-Token` del broker en marcha: variable de entorno,
   llavero del sistema, fichero, o se genera en cada arranque?
2. ¿Cambia en cada arranque o es persistente?
3. Si es persistente: ¿cuál es el nombre exacto del servicio/usuario en el llavero (o de la
   variable), y puedo tratarlo como parte del contrato, es decir, contar con que no cambie
   sin aviso?
4. Si se genera en cada arranque: ¿hay un mecanismo *local* previsto para que un proceso
   co-ubicado lo recupere sin que aparezca en logs ni en línea de comandos?
5. ¿Qué debe hacer un cliente ante un `401`/`403` tras un reinicio del broker: volver a leer
   la fuente y reintentar una vez es el comportamiento correcto?

**Por qué importa ahora.** El plan de Agora contempla encender el PC de IA por Wake-on-LAN.
En ese escenario el broker lo arranca Windows solo, no Agora, así que la única vía posible
es que Agora **lea** la credencial de una fuente local acordada. Necesito saber cuál es esa
fuente antes de implementarlo, y no quiero deducirla del código.

---

## Resumen para priorizar

| # | Petición | Esfuerzo estimado | Bloquea a Agora |
|---|---|---|---|
| 5 | Confirmar el ciclo de vida del `X-Admin-Token` | Solo responder | **Sí** — bloquea el runner en el PC de IA y el Wake-on-LAN |
| 2 | Opt-out de invocaciones auxiliares + documentar garantías | Medio | **Sí** para tarjetas estrictas o confidenciales |
| 1 | `enum` de `role`/`status` + `contractual: bool` | Bajo | No, pero hoy lo suplo con una lista negra frágil |
| 3 | Eco del `prompt_compression` efectivo | Bajo | No, pero impide una prueba que la especificación exige |
| 4 | Declarar `/artifacts` como vía canónica del entregable | Muy bajo (documentación) | No |

Si hay que empezar por uno, que sea el **5**: es solo responder cinco preguntas y es lo
único que tiene a Agora parado.
