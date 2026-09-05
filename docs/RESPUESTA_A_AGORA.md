# Respuesta a la petición de Agora — contrato 2.10 (2026-09-03)

Las cinco peticiones están atendidas. Todo lo que sigue es **aditivo**: un cliente
de 2.9 sigue funcionando sin tocar una línea. Lo que cambia es lo que puedes
**probar** después.

`GET /api/v1/capabilities` responde ahora `contract_version: "2.10"`, y el detalle
de cliente está en `docs/Client_API.md` (§3.1, §8.1, §8.3, §8.4, §8.5 y §12).

Primero, dos cosas que conviene decir de entrada.

**Tenías razón en el punto 2, y era más grave de lo que se veía desde fuera.** El
sondeo comparte con la selección real toda la cadena de elegibilidad —clasificación
de datos, `cloud_allowed`, `allowed_providers`, capacidades, ventana— pero esa
cadena no mira `target_model`: ahí solo se decide quién *podría* servir, no quién
sirve. El resultado es exactamente el que mediste: la letra del contrato cumplida
y el trato roto. No hacía falta un opt-out para arreglarlo, y por eso hay dos
respuestas y no una.

**Y tu punto 5 no tenía respuesta que dar: había que construirla.** El token no
salía de ningún sitio que un proceso pudiera leer. Ver más abajo.

---

## 1. `role` y `status` enumerados, y un booleano que no depende del nombre

**Hecho, las dos cosas.**

`TaskInvocationItem.role` y `.status` llevan `enum` en el OpenAPI:

| Campo | Valores |
|---|---|
| `role` | `single`, `agent`, `proposer`, `generalist`, `specialist`, `skeptic`, `analyst`, `reviewer`, `refiner`, `arbiter`, `chunk_map`, `chunk_reduce`, `confidence_judge`, `vision`, `shadow_probe`, `unknown` |
| `status` | `started`, `completed`, `failed`, `ambiguous`, `unknown` |

Nota: el vocabulario real tenía **catorce** roles, no tres. Los tres que viste son
los que produce una tarea `single` con el sondeo activo; una `mixture_of_agents`
saca cinco nombres de proposer más el árbitro, y map-reduce saca `chunk_map` /
`chunk_reduce`. Tu lista negra se habría roto antes de que añadiéramos nada.

`unknown` no lo escribe el broker nunca. Es la red de seguridad para filas que
haya escrito una versión con más vocabulario que la que responde (una vuelta
atrás sobre la misma BD): preferimos decir "no lo reconozco" a devolverte un 500
en la telemetría.

Y cada invocación trae **`contractual: bool`**:

- `false` → trabajo propio del broker. No cuenta para tu factura ni para validar
  tu política. Hoy solo `shadow_probe`.
- `true` → trabajo tuyo, aunque no sea la llamada que entrega la respuesta.

**Una corrección sobre tu lista negra:** `confidence_judge` **sí es tuyo**. Es el
meta-router puntuando la respuesta para decidir si escala a consenso; hereda tu
`model_requirements` entero, así que corre en tu `target_model`, y lo pagas. Al
apartarlo estabas infravalorando el coste de esas tarjetas. Con el booleano eso
se arregla solo.

```bash
curl -s -H "X-Admin-Token: $TOKEN" \
     http://192.168.1.52:8765/api/v1/tasks/$TASK/invocations \
  | jq '[.items[] | select(.contractual)] | {coste: (map(.cost_usd) | add),
        modelos: (map(.model.model) | unique)}'
```

---

## 2. Invocaciones auxiliares: opt-out explícito **y** garantía implícita

**Hecho, con las tres cosas que pediste.**

### a) La garantía que no tienes que pedir

Una tarea con `model_requirements.target_model` **y** `fallback_allowed: false`
ya no se sondea. Declarar un modelo exacto sin fallback es declarar que solo ese
modelo puede ver el contenido, y respetarlo únicamente en la invocación que
responde era cumplir la letra del contrato y no el trato. Tus tarjetas estrictas
actuales quedan cubiertas **sin migrar nada**.

### b) El opt-out explícito

```json
{ "auxiliary_invocations": false }
```

Campo de primer nivel, default `true`. Vale para cualquier tarea, la fije o no a
un modelo concreto — que es el caso que la garantía implícita no cubre: una
tarjeta que quiere exclusividad sin querer fijar el modelo.

**No lo colgamos de `exclude_from_model_learning`**, aunque encajaba. Son dos
promesas distintas: aquella dice "no aprendas de esta tarea" y afecta a todas las
métricas del router; esta dice "solo el modelo aprobado ve este contenido". Si
mañana el broker aprende de otra forma, la segunda tiene que seguir en pie por sí
sola. (Como efecto colateral: `exclude_from_model_learning: true` **ya** apagaba
el sondeo antes de este cambio, y eso no estaba documentado. Ahora sí lo está,
pero no te apoyes en ello — apóyate en el campo que promete lo que quieres.)

### c) Las garantías, escritas

En `docs/Client_API.md` §8.4, con una tabla de qué respeta y qué no:

| | |
|---|---|
| `risk.data_classification` | **Sí.** Una tarea `confidential`/`local_only` solo sondea modelos locales |
| `cloud_allowed` | **Sí**, fail-closed |
| `allowed_providers` | **Sí** |
| Ventana y capacidades | **Sí**: misma cadena de elegibilidad que la selección real |
| `target_model` a secas | **No.** Con fallback permitido es una preferencia de enrutado |
| Tu presupuesto | No lo consume: el sondeo no se factura a tu tarea |

Lo que observaste era correcto, y ahora está por escrito en vez de por
observación.

### d) Y en `capabilities`

- `auxiliary_invocations: bool` — si **este** broker las hace. Refleja la
  configuración del operador: puede tenerlas apagadas.
- `auxiliary_invocations_optout: bool` — si acepta que las apagues.

Son dos preguntas distintas a propósito: un cliente que no vea la segunda debe
rechazar sus tarjetas estrictas, no confiar en un campo que se ignora.

---

## 3. Eco de `prompt_compression`

**Hecho, en la invocación** (que era tu segunda opción, y es la correcta —
explico por qué).

```json
"prompt_compression": {"requested": "off", "effective": "off"}
```

Va junto a `generation`, `execution_fingerprint` y `content_source`, capturado al
abrir el checkpoint igual que la huella.

**Por qué en la invocación y no en `execution_summary`:** el valor de la tarea no
describe lo que le pasó a cada llamada. El broker fuerza `off` en las invocaciones
que procesan contenido generado —fragmentos de map-reduce, síntesis de segunda
ronda, el juez de confianza—, porque comprimir texto que ya salió de un modelo
puede romperle un tag o comerle cifras. Un eco a nivel de tarea habría dicho una
cosa por seis llamadas distintas.

**Y un matiz que desde fuera no se ve:** `requested` y `effective` no siempre
coinciden. `aggressive` se degrada a `medium` cuando el prompt viaja en un loop de
tools o hay `output.language` fijado (que lo está por defecto, así que el modo
caveman no se aplica nunca por política global). `effective: "off"` cubre además
la compresión global apagada y el prompt por debajo del mínimo de caracteres:
decir `medium` ahí sería anunciar una poda que no ocurrió.

`requested: "broker_default"` = no te pronunciaste y mandó la configuración del
broker. Lo nombramos en vez de dejarlo en `null` para que distingas "no pedí
nada" de "pedí y no se me aplicó".

Tu prueba automática:

```bash
jq -e 'all(.items[] | select(.contractual); .prompt_compression.effective == "off")'
```

---

## 4. `/artifacts` es la vía canónica, y el entregable viene marcado

**Hecho — y algo más de lo que pediste, porque documentarlo a secas te dejaba otra
lista a mano.**

Te valía con la opción de documentación. El problema: `/artifacts` mezcla el
entregable con lo que lo acompaña (una imagen generada), y el tipo del entregable
cambia con la estrategia — `single_output`, `agent_output`, `synthesis_output`,
`embedding_output`. Documentar esa lista te habría dejado exactamente la lista
negra del punto 1 con otro nombre.

Así que cada artefacto declara **`final: bool`**. Una tarea completada tiene
exactamente uno, sea cual sea la estrategia. Filtra por el booleano.

```bash
jq '.items[] | select(.final) | {artifact_id, sha256, download_url}'
```

Documentado en §8.3 como la vía canónica, y en §8 («El resultado») queda dicho
que `result` **no va a tiparse**: se queda como está por compatibilidad, y para
cada cosa hay una vía con contrato — `execution_summary` para quién sirvió,
`/invocations` para lo que costó, `/artifacts` para el entregable. Migra a
`/artifacts` como tenías previsto.

**Un detalle que encontramos al hacerlo:** el artefacto se escribía en modo texto,
así que en Windows cada salto de línea se guardaba como CRLF y el fichero no era
byte a byte lo que produjo el modelo. El `sha256` salía coherente —se calcula
releyendo el fichero— pero cerraba sobre una copia reescrita por la plataforma.
Corregido: ahora el hash cierra sobre la respuesta. Los artefactos ya escritos no
se tocan.

---

## 5. El `X-Admin-Token`: tus cinco preguntas

Aquí no había respuesta que dar. Las respondo una a una y luego digo qué hemos
construido.

**1. ¿De dónde sale?** Se **genera en cada arranque** (`secrets.token_urlsafe(24)`
en `scripts/run_broker.py`), se publica en la variable de entorno
`AI_BROKER_ADMIN_TOKEN` **del propio proceso del broker** y se imprime en su
consola. El keyring (`ai-broker` / `dashboard_admin_token`) solo se consulta como
*fallback*, y hasta ahora nadie escribía ahí.

**2. ¿Cambia en cada arranque?** Sí. Excepción: si `AI_BROKER_ADMIN_TOKEN` ya
viene definida desde fuera, se respeta ese valor — es la vía para fijar uno
estable.

**3. Si es persistente…** No lo es, por defecto.

**4. ¿Hay un mecanismo local para un proceso co-ubicado?** **No lo había.** Esa es
la respuesta honesta a lo que preguntabas: la variable moría con el proceso y la
consola en tu escenario no la mira nadie. Ahora lo hay.

Con `server.publish_session_token: keyring` (ya activado en el `broker_config.yaml`
de esta máquina), cada arranque deja el token de esa sesión en el Administrador de
credenciales de Windows:

| | |
|---|---|
| Servicio | `ai-broker` |
| Usuario | `session_admin_token` |

```python
import keyring
token = keyring.get_password("ai-broker", "session_admin_token")
```

Cifrado, con ACL del usuario que corre el broker, fuera de logs y de líneas de
comando, y sin cruzar la red. **Los dos nombres son parte del contrato**: no
cambian sin nota en §12 de `Client_API.md`.

**No leas `ai-broker/dashboard_admin_token`.** Es una entrada distinta, del
operador, para fijar un token estable; el broker la lee como fallback y nunca la
escribe. Pisarla con el token efímero convertiría un token de sesión en uno
permanente, y por eso son dos entradas separadas.

**5. ¿Qué hacer ante un 401/403?** Volver a leer la fuente y reintentar **una**
vez: sí, es el comportamiento correcto. Casi siempre significa que el broker se
reinició y rotó el token. Si el segundo intento falla, es un problema real de
credencial, no un reinicio — no entres en bucle. Y tus tareas siguen ahí: una en
`waiting_for_tools` sobrevive intacta al reinicio.

**Sobre Wake-on-LAN:** el publicado ocurre en `create_app`, no en el script de
arranque, precisamente para que dé igual cómo se lance el broker (el `.bat`,
uvicorn directo, un servicio de Windows). Si el llavero falla, el broker arranca
igual y deja un warning `admin.session_token_publish_failed` en el log: el síntoma
en tu extremo serían `403` sin explicación, y eso no puede pasar callando.

**Descartamos el fichero local con ACL.** Sería el mismo secreto en claro, con
permisos que dependen de dónde caiga el directorio y sobreviviendo a un apagado
sucio. Si tu runner no puede usar `keyring`, dilo y lo hablamos.

---

## Qué tienes que hacer en Agora

Por orden de valor:

1. **Borra la lista negra de roles.** Usa `contractual`, y vuelve a sumar
   `confidence_judge` a la factura de las tarjetas que lo usen.
2. **Lee el token de `ai-broker` / `session_admin_token`**, con relectura +
   un reintento ante `403`. Con eso el runner y la pasarela dejan de estar
   bloqueados.
3. **Migra a `/artifacts` con `final: true`** para cerrar tarjetas.
4. **Escribe la prueba de compresión** sobre `prompt_compression.effective`.
5. **Nada, para las tarjetas estrictas.** Ya están cubiertas por la garantía
   implícita. Usa `auxiliary_invocations: false` solo donde quieras
   exclusividad sin fijar el modelo.

Y al arrancar, comprueba `capabilities.contract_version == "2.10"`,
`auxiliary_invocations_optout`, `invocation_contract`,
`prompt_compression_echo` y `canonical_artifacts`. Si alguno falta, estás
hablando con un broker anterior y tus tarjetas estrictas deben rechazarse ahí,
no al leer la telemetría.
