# Fase 8 — Sandbox de ejecución de código (skill `run_code`)

Fecha: 19 de julio de 2026

El broker puede delegar la ejecución de código Python generado por los modelos
a un **contenedor Docker desechable**. Decisión de diseño: el broker sigue
nativo en Windows (necesita Ollama/LM Studio y la GPU del host); lo que se
aísla es cada ejecución de código no confiable, no el broker.

## La skill

`run_code` se suma a las skills del agente (`web_search`, `fetch_url`,
`calculator`, `current_datetime`) y también está disponible para los
proponentes de mixture (`proposer_skills`). El modelo recibe la definición de
tool con el contrato claro: entorno efímero y sin estado, sin red, sin
ficheros del host, límites de tiempo/memoria, y debe usar `print()` para
devolver resultados.

**Opt-in en dos niveles:**
- `run_code` NO está en las skills por defecto de una tarea agent
  (`DEFAULT_AGENT_SKILLS`); la tarea debe pedirla explícitamente.
- Requiere `sandbox.enabled` en la configuración. Sin sandbox, crear una tarea
  que la pida devuelve `409 SANDBOX_DISABLED` (API) o error en página
  (probador), y la casilla ni aparece en el probador.

`/api/v1/capabilities` expone `sandbox_run_code` y solo incluye `run_code` en
`agent_skills` cuando el sandbox está activo.

## Aislamiento (fronteras NO configurables)

`app/sandbox.py` (`SandboxExecutor.run_python`) lanza por ejecución:

```
docker run --rm -i --name ai-broker-sbx-<uuid>
  --network none                     # sin exfiltración posible
  --memory Nm --memory-swap Nm       # sin swap extra
  --cpus N --pids-limit N
  --read-only                        # rootfs inmutable
  --tmpfs /work --tmpfs /tmp         # únicos escribibles, mueren con el contenedor
  --user 65534:65534                 # nobody
  --cap-drop ALL --security-opt no-new-privileges
  python:3.12-slim timeout <T>s python -I -B -
```

- El código viaja por **stdin** (sin problemas de quoting/longitud en Windows).
- **Sin volúmenes del host**: no se monta nada, jamás.
- Timeout en dos capas: `timeout` dentro del contenedor (exit 124 → mensaje
  claro al modelo) y plazo asyncio fuera con margen de arranque; si el CLI
  muere sin limpiar, `docker rm -f` remata el contenedor por nombre.
- Salida truncada a `max_output_chars`; exit code y stderr se reportan al
  modelo como parte del resultado (puede corregir su código y reintentar).
- Docker parado/ausente → `SandboxError` → la skill devuelve un error de tool
  legible; la tarea no revienta y el broker no se ve afectado.

## Configuración (`broker_config.yaml`)

```yaml
sandbox:
  enabled: true
  docker_path: docker
  image: python:3.12-slim     # pull ya hecho en esta máquina
  timeout_seconds: 60.0
  memory_mb: 1024
  cpus: 2.0
  pids_limit: 256
  max_output_chars: 8000
```

Requisito operativo: **Docker Desktop en marcha** (WSL2). Si no lo está, la
skill falla limpia con "Docker no responde (¿Docker Desktop arrancado?)".

## Verificación

`tests/test_sandbox.py` (12 casos unit/API) + integración real opt-in
(`AI_BROKER_SANDBOX_DOCKER=1`), ejecutada y en verde en esta máquina:
`print(6*7)` → 42; conexión a 1.1.1.1:80 → bloqueada; escritura en `/etc` →
bloqueada.

## Amenaza cubierta y límites honestos

Cubre: código malicioso o defectuoso generado por LLMs (borrar ficheros,
exfiltrar datos, bucles infinitos, bombas de memoria/procesos). Un contenedor
comparte kernel con la VM de WSL2, así que no es una VM de aislamiento
perfecto contra exploits de kernel — para el modelo de amenaza real de este
broker (código de LLMs locales, un solo operador) es la relación
seguridad/latencia correcta. Escapar exigiría un 0-day de kernel; y aun
escapando, el contenedor no tiene red.

## Imagen de análisis de datos (2026-07-19)

`sandbox/Dockerfile` → `ai-broker-sandbox:latest` (513 MB): python:3.12-slim
+ numpy, pandas, matplotlib (backend Agg), openpyxl. Configurada como imagen
por defecto en el YAML del usuario y verificada en vivo (agregación pandas
dentro del aislamiento). Reconstrucción: `docker build -t
ai-broker-sandbox:latest sandbox/`. El aislamiento lo impone el ejecutor en
tiempo de ejecución, no la imagen.

## Configurable desde el dashboard (2026-07-19)

Secciones "Sandbox de código" e "Ingesta de ficheros" en Configuración, con
el mismo guard de presencia que los proveedores (un formulario sin la sección
no toca esa parte del YAML) y entradas en la revisión de cambios. El
`SandboxExecutor` se construye siempre y lee `sandbox.*` en vivo del
BrokerConfig compartido (mismo idioma que ResourceScheduler): activar o
desactivar el sandbox desde el panel aplica sin reiniciar el broker. Campos
expuestos: enabled/imagen/binario/timeout/memoria/CPUs (sandbox) y
enabled/OCR/figuras/transcripción/límites/endpoint+modelo de visión/modelo
Whisper/ruta ffmpeg (ingesta); el resto sigue en YAML.

## Posibles siguientes pasos

- Mover la conversión de documentos de la ingesta (docling/markitdown) al
  sandbox para aislar también el parseo de ficheros hostiles (coste real:
  imagen de varios GB con torch y latencia extra; evaluar cuando duela).
