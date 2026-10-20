# PC IA: lo que hay que hacer para que nadie pegue tokens (AI OS Core 0.4.0)

Desde el 19-oct-2026 el token del AI Broker lo reparte AI OS Core a todas las apps del PC
principal. Lo recibe del runner de Agora, que vive en el PC IA junto al Broker y lo lee del
llavero de Windows. En el código del Broker **no hay que cambiar nada**. En el PC IA hay que
hacer esto:

## 1. Broker: comprobar que publica su token en el llavero (sin cambios de código)

En la configuración del Broker tiene que estar:

```yaml
server:
  publish_session_token: keyring
```

Al arrancar, el Broker deja su token de sesión en el llavero de Windows del usuario con el que
corre: servicio `ai-broker`, cuenta `session_admin_token` (Client_API.md, 3.1). El runner de
Agora ya lo lee de ahí desde la 0.2.x. **El runner tiene que correr con el mismo usuario de
Windows que el Broker**, o no verá esa entrada.

Comprobación (en el PC IA, con el Python del runner):

```
%LOCALAPPDATA%\Agora\runner\venv\Scripts\python.exe -c "import keyring; print(bool(keyring.get_password('ai-broker','session_admin_token')))"
```

(El instalador del runner lo deja en `%LOCALAPPDATA%\Agora\runner`, no en `C:\Agora\venv`.)

Tiene que imprimir `True` con el Broker arrancado.

## 2. Runner: actualizar a 0.2.15 y conectarlo a Core por HTTPS (un fichero, doble clic)

1. En el PC principal, el usuario ejecuta `AI_OS_core\Abrir Core a la red.bat` (pide permiso de
   administrador). Abre el puerto 8443 (HTTPS) solo a la red local y genera
   `AI_OS_core\dist\Conectar runner a Core.bat`, que ya incluye el runner 0.2.15, la URL
   `https://<IP del PC principal>:8443` y el token del runner ante Core.
2. Ese fichero se copia al PC IA y se le hace doble clic. Para la tarea del runner, instala la
   0.2.15, la vuelve a registrar con `--core-url https://…:8443` y `AGORA_CORE_TOKEN`, la
   arranca y hace `--check`.
3. **Borrar el fichero después**: lleva el token del runner.

Qué hace el runner 0.2.15 de nuevo:
- En cada vuelta lee el token del llavero. Si ha cambiado (el Broker se reinició), o si ha
  pasado un minuto (Core pudo reiniciarse y lo guarda solo en memoria), lo envía a Core:
  `PUT /api/v1/credentials/broker`.
- Comprueba el certificado HTTPS de Core con la CA de Agora que ya usa para el tablero
  (`--ca-cert`). No hace falta copiar ningún certificado nuevo.
- Si Core no contesta, no se detiene: lo vuelve a intentar en la vuelta siguiente.

En el log del runner tiene que aparecer `AI OS Core: token del broker publicado` al arrancar
y cada vez que el Broker se reinicie.

## 3. HTTPS también en el Broker

Lo anterior cifra el tramo runner → Core, que es por donde viaja el token. El tráfico
apps → Broker (puerto 8765) va en HTTP dentro de la red local, y la cabecera
`X-Admin-Token` viaja en claro. El Broker sabe servir HTTPS (`server.tls_certfile` /
`server.tls_keyfile`, ver Configuration_Reference.md) con un certificado de la CA propia del
Broker («AI Broker CA»; su clave solo está en el PC principal):

1. **En el PC principal** se emiten `ca.crt`, `server.crt` y `server.key` (válidos para
   `192.168.1.52`, `127.0.0.1` y `localhost`) y se copian al PC IA en `C:\Agora\broker-https\`.
2. **En el PC IA**, doble clic en `C:\Procesos\AI_Broker\activar_https_broker.bat`. Comprueba
   el par contra esa CA para la IP y para loopback; si no vale, no cambia nada. Si vale, los
   copia a `certs\`, pone `server.host: 0.0.0.0`, las rutas del certificado y
   `tls_loopback_http: false` en `broker_config.yaml` (con copia de seguridad), deja la clave
   legible solo para el usuario, reinicia el Broker y prueba HTTPS en la red y en loopback.
3. **Runner de Agora**: `"Actualizar runner Agora 0.2.16.bat" -BrokerHttps` lo pasa a
   `https://127.0.0.1:8765` con `--broker-ca-cert`.

Resultado: **solo HTTPS**, `https://192.168.1.52:8765` en la red y `https://127.0.0.1:8765`
dentro del PC IA. Ningún HTTP contesta en el puerto 8765.

**Después, en el PC principal (lado de las apps):** `AI_OS_core\Pasar apps a Broker HTTPS.bat`
cambia la URL del Broker a `https://192.168.1.52:8765` y hace que cada cliente confíe en la
CA del Broker para esa URL (`verify=<ca.crt del Broker>` en httpx o requests). No se hace con
`SSL_CERT_FILE` global, porque rompería el HTTPS de internet (por ejemplo, Google Calendar en
Gestión Tareas IA). En cuanto el paso 2 esté hecho, cualquier app que siga en
`http://192.168.1.52:8765` dejará de conectar. Conviene hacer los dos cambios seguidos.
