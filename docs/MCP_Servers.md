# Servidores MCP en el AI Broker

## Objetivo

Dar al bucle agéntico herramientas de terceros —sistemas de ficheros, bases de datos, APIs internas— sin escribir un adaptador por cada una. El broker actúa como **cliente MCP**: lanza el servidor como subproceso, descubre sus herramientas y las ofrece al modelo junto a las skills propias.

Off por defecto (`mcp.enabled: false`). Sin servidores configurados, nada de esto existe.

## Tres fronteras que no se negocian

**Solo transporte stdio.** No hay cliente HTTP/SSE. El broker no depende de ninguna nube para funcionar, y un servidor remoto convertiría cada herramienta en una salida de datos menos visible que un proveedor cloud —justo lo contrario de lo que el producto promete—. Un servidor MCP es un proceso que corre en esta máquina.

**La frontera de datos la declara el operador, servidor por servidor, y es obligatoria.** `data_boundary` no tiene valor por defecto: sin él la configuración no valida. Deducirla del transporte sería cómodo y mentiría en el caso que importa —un servidor stdio que por dentro llama a una API de internet quedaría marcado como local— y con eso `local_only` volvería a prometer una frontera que no aplica. Quien añade el servidor sabe lo que hace por dentro; el broker no puede saberlo, así que lo pregunta en vez de suponerlo.

**Los nombres van con prefijo.** Una herramienta `buscar` del servidor `docs` se le ofrece al modelo como `mcp__docs__buscar`. Sin espacio de nombres, un servidor podría publicar `web_search` y suplantar a la skill del broker o a una tool del cliente dentro de la misma llamada al modelo.

## Configuración

```yaml
mcp:
  enabled: true
  startup_timeout_seconds: 20.0
  servers:
    - id: ficheros              # [a-z0-9][a-z0-9_-]*, único
      enabled: true
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/Procesos/notas"]
      data_boundary: local      # OBLIGATORIO: local | egress
      timeout_seconds: 30.0
    - id: jira
      command: python
      args: ["-m", "mi_servidor_jira"]
      env:
        JIRA_TOKEN: "env:MI_JIRA_TOKEN"   # toma el valor del entorno del broker
      data_boundary: egress     # habla con un servicio externo
```

`env` con el prefijo `env:` lee el valor de una variable de entorno del broker en vez de escribirlo en el YAML, igual que `api_key_env` en los proveedores. Una referencia que no existe se omite en lugar de propagar una cadena literal `env:...` al proceso hijo.

## Uso desde una tarea

Opt-in explícito por tarea:

```json
{
  "execution": {
    "strategy": "agent",
    "agent": { "skills": ["calculator"], "mcp_servers": ["ficheros"] }
  }
}
```

Un servidor configurado **no** se cuela en todas las tareas. Cada herramienta añadida es contexto que el modelo tiene que leer en cada iteración y una forma más de que la investigación se vaya por las ramas, así que se pide.

Sirve también para los proponentes de un `mixture_of_agents`: si hay `mcp_servers`, los proponentes corren el bucle de tools aunque no se les hayan dado `proposer_skills`, porque las herramientas de terceros ya son motivo suficiente para tener bucle. **El árbitro nunca usa herramientas**, como con las skills.

`GET /api/v1/capabilities` publica `mcp_servers: [{id, data_boundary}]` para que un cliente sepa qué hay y cuál puede usar bajo frontera local.

## Qué se rechaza y cuándo

Al **crear** la tarea (`409`), nunca al ejecutarla, porque todo esto depende de la configuración del broker y no del contrato:

| Código | Cuándo |
|---|---|
| `MCP_DISABLED` | `mcp.enabled: false` y la tarea pide servidores |
| `MCP_SERVER_UNKNOWN` | El id no está configurado o está desactivado |
| `MCP_SERVER_NOT_LOCAL` | La tarea es `confidential`/`local_only` y pide un servidor `egress` |

El último es la misma regla que ya rige para `web_search` y `fetch_url` (ver [`Client_API.md`](Client_API.md) §4), con una diferencia de implementación: las skills se rechazan en el contrato porque su frontera es una constante del propio contrato; los servidores MCP se rechazan en el endpoint porque su frontera vive en el YAML y el esquema Pydantic no la conoce.

## Qué pasa cuando algo falla

**Un servidor que no arranca no tumba nada.** Aporta cero herramientas, deja un `mcp.server_unavailable` en el log y la tarea corre con lo que sí funciona. Que una integración opcional esté rota no puede impedir que se responda.

**Un error de una herramienta es texto para el modelo**, no una excepción: llega como resultado de tool con el prefijo `ERROR:` y el agente puede reaccionar y probar otra cosa. Misma convención que `run_skill`, y por el mismo motivo. Solo un fallo de proceso o de protocolo corta la llamada, y aun así como texto de error.

**Los procesos se arrancan de forma perezosa** —la primera tarea que pida ese servidor— y se mantienen vivos entre tareas: el handshake cuesta y una tarea agéntica llama varias veces seguidas. Se cierran al parar el broker; sin eso quedarían huérfanos.

**Ruido en stdout.** Muchos servidores escriben avisos en stdout en vez de stderr. El cliente descarta las líneas que no son JSON hasta un tope en lugar de romper el handshake.

## Lo que el modelo recibe

El resultado de `tools/call` se convierte a texto: los bloques `text` se concatenan, los `resource` aportan su texto o su URI, y el contenido no textual (imágenes, audio) se nombra como omitido en vez de inventar una transcripción. Truncado a 8.000 caracteres, como las skills.

Ese texto es **dato externo no confiable**, igual que el de cualquier skill: el system prompt del agente ya lo declara así (ver [`System_Prompts.md`](System_Prompts.md)).

## Límites actuales

- **Solo `tools/`.** No se usan `resources/` ni `prompts/` del protocolo MCP.
- **Sin reintento de servidor caído dentro de una tarea**: si el proceso muere a mitad, las llamadas siguientes fallan con texto de error hasta que otra tarea lo vuelva a levantar.
- **Sin sondeo de salud** de los servidores en el panel: su estado solo se ve en el log y en el resultado de las llamadas.
- El catálogo de herramientas se descubre una vez por proceso; un servidor que cambie sus herramientas en caliente no se relee hasta que el proceso se reinicie.

## Tests

`tests/test_mcp.py` levanta un servidor MCP de verdad como subproceso (`tests/fixtures/mcp_echo_server.py`): no se simula el transporte, porque los fallos que importan están justo ahí — framing por líneas, ruido en stdout, procesos que no arrancan. Cubre descubrimiento, llamada, error de herramienta frente a error de protocolo, servidor roto, la frontera de datos y el recorrido completo desde una tarea.
