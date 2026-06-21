# Deployment Guide: YouTube Knowledge Pipeline

> **Precedencia:** `Despliegue Normativo del MVP` sustituye rutas, comandos y valores anteriores cuando difieran.



## Requisitos del Sistema



### Máquina Principal (Orchestrator)

- **OS:** Windows 11

- **RAM:** 8GB mínimo, 16GB recomendado

- **Storage:** 10GB libres para archivos temporales

- **Python:** 3.10 o superior

- **Conectividad:** Acceso a red local donde está el Broker



### Máquina del Broker IA (LLMs locales)

- **OS:** Windows 11 / Linux

- **RAM:** 64GB (ampliable a 128GB según especificaciones)

- **GPU:** NVIDIA con 64GB+ VRAM

- **Storage:** 500GB+ SSD para modelos

- **Ollama:** Instalado y configurado



### Navegador

- **Chrome:** Versión 88+ (soporte Manifest V3)

- **Edge:** Versión 88+ (opcional)

- **Firefox:** Versión 109+ (opcional)



## Instalación Paso a Paso



### 1. Preparación del Entorno



#### Crear Estructura de Directorios

```bash

# En máquina principal

mkdir C:\\YT-Pipeline

mkdir C:\\YT-Pipeline\\state

mkdir C:\\YT-Pipeline\\processing

mkdir C:\\YT-Pipeline\\completed

mkdir C:\\YT-Pipeline\\failed

mkdir C:\\YT-Pipeline\\rejected

mkdir C:\\YT-Pipeline\\logs



# Crear carpetas en Obsidian Vault

mkdir "C:\\ObsidianVault\\Knowledge\\IA-y-LLMs"

mkdir "C:\\ObsidianVault\\Knowledge\\Desarrollo"

mkdir "C:\\ObsidianVault\\Knowledge\\Trading"

mkdir "C:\\ObsidianVault\\Knowledge\\_inbox"

```



#### Configurar Variables de Entorno

```bash

# Crear archivo .env en carpeta del Broker

DEEPSEEK_API_KEY=tu_api_key_aqui

OLLAMA_URL=http://localhost:11434

```



### 2. Instalación del Plugin Chrome



#### Modo Desarrollador (Testing)

1. Abrir `chrome://extensions/`

2. Activar **"Modo desarrollador"** (toggle superior derecha)

3. Clic en **"Cargar extensión sin empaquetar"**

4. Seleccionar carpeta `YT_Capture_Plugin/`

5. Verificar que aparece en la lista de extensiones



#### Verificación de Funcionamiento

1. Ir a cualquier video de YouTube

2. Clic en el icono de la extensión

3. Verificar que se muestran los datos del video

4. Probar captura y verificar archivo en Descargas



### 3. Instalación del AI Broker



#### En la Máquina de LLMs

```bash

# Clonar o copiar archivos del Broker

cd C:\\AI-Broker



# Crear entorno virtual

python -m venv venv

venv\\Scripts\\activate



# Instalar dependencias

pip install fastapi uvicorn httpx pyyaml jinja2 python-multipart structlog psutil python-dotenv



# Configurar Ollama (si no está instalado)

# Descargar desde https://ollama.ai/

ollama pull llama3.1:8b

ollama pull llama3.1:70b



# Verificar modelos disponibles

ollama list

```



#### Configurar el Broker

```yaml

# Editar broker_config.yaml

server:

  host: "IP_LAN_DEL_BROKER"  # No exponer a Internet

  port: 8080



ollama:

  url: "http://localhost:11434"



external_apis:

  deepseek:

    api_key: "tu_deepseek_api_key"

    monthly_budget: 5.00

```



#### Ejecutar el Broker

```bash

# Desde la carpeta del Broker

python -m app.main



# O usando uvicorn directamente

uvicorn app.main:app --host IP_LAN_DEL_BROKER --port 8080 --workers 1

```



#### Verificar Funcionamiento

1. Abrir `http://IP_MAQUINA_BROKER:8080/` en navegador

2. Verificar que aparece el dashboard

3. Comprobar que se listan los modelos de Ollama

4. Probar endpoint: `http://IP_MAQUINA_BROKER:8080/api/v1/models`



### 4. Instalación del Knowledge Orchestrator



#### En la Máquina Principal

```bash

# Crear entorno para el Orchestrator

cd C:\\Knowledge-Orchestrator

python -m venv venv

venv\\Scripts\\activate



# Instalar dependencias

pip install customtkinter watchdog httpx pyyaml markdown matplotlib

```



#### Configurar el Orchestrator

```yaml

# Editar config.yaml

paths:

  inbox: "%USERPROFILE%/Downloads/YT-Knowledge-Inbox"

  obsidian_vault: "C:/ObsidianVault/Knowledge"



broker:

  hostname: "IP_DE_TU_MAQUINA_BROKER"  # Cambiar por IP real

  port: 8080



topics:

  - name: "IA-y-LLMs"

    folder: "IA-y-LLMs"

    keywords: ["ia", "llm", "gpt", "ollama"]

```



#### Ejecutar el Orchestrator

```bash

# Desde la carpeta del Orchestrator

python -m orchestrator.main

```



#### Verificar Funcionamiento

1. Debe aparecer la ventana de la aplicación

2. En el Dashboard, verificar estado del Broker (🟢 verde)

3. Comprobar que se pueden ver los modelos disponibles

4. Verificar que la pestaña "Cola" muestra el pipeline visual



## Configuración de Red



### Encontrar IP de la Máquina Broker

```bash

# En Windows (máquina del Broker)

ipconfig



# Buscar la IP en la sección "Adaptador Ethernet" o "Wi-Fi"

# Ejemplo: 192.168.1.50

```



### Configurar Firewall (si es necesario)

```bash

# En máquina del Broker, permitir puerto 8080

# Windows Defender Firewall > Reglas de entrada > Nueva regla

# Tipo: Puerto

# Puerto: 8080

# Acción: Permitir conexión

```



### Verificar Conectividad

```bash

# Desde máquina principal, probar conexión

curl http://192.168.1.50:8080/health



# Debe devolver: {"status": "ok"}

```



## Flujo de Testing Completo



### Test End-to-End

1. **Captura:** Usar plugin en video de YouTube → verificar archivo en Descargas

2. **Entrega:** Verificar que aparece en `Descargas\\YT-Knowledge-Inbox\\`; el Orchestrator vigila esa carpeta

3. **Procesamiento:** Verificar que Orchestrator detecta el archivo

4. **Cola:** Ver el archivo aparecer en la cola visual del Orchestrator

5. **Broker:** Verificar en dashboard del Broker que aparece la tarea

6. **Resultado:** Comprobar que se genera archivo final en Obsidian



### Test de Cada Componente



#### Plugin Chrome

```javascript

// Abrir DevTools en popup de extensión

// Verificar que no hay errores en consola

// Probar con diferentes videos (con/sin transcripción)

```



#### Broker IA

```bash

# Test directo de API

curl -X POST http://192.168.1.50:8080/api/v1/extract \\

  -H "Content-Type: application/json" \\

  -d '{"task_id": "test", "profile": {...}, "content": {...}}'

```



#### Orchestrator

- Verificar logs en `C:\\YT-Pipeline\\logs\\`

- Comprobar que archivos se mueven entre carpetas correctamente

- Verificar generación correcta de frontmatter en Obsidian



## Solución de Problemas Comunes



### Plugin No Funciona

- **Verificar:** Extensión habilitada en `chrome://extensions/`

- **Permisos:** Comprobar que tiene acceso a `*.youtube.com`

- **Console:** Abrir DevTools en popup, buscar errores JavaScript



### Orchestrator No Ve el Broker

- **Red:** Verificar IP y puerto en `config.yaml`

- **Firewall:** Comprobar que puerto 8080 está abierto

- **Broker:** Verificar que está ejecutándose (`curl http://IP:8080/health`)



### Modelos No Disponibles

- **Ollama:** Verificar que está ejecutándose (`ollama list`)

- **VRAM:** Comprobar memoria GPU disponible

- **Logs:** Revisar logs del Broker para errores de carga de modelos



### Archivos No Se Procesan

- **Permisos:** Verificar permisos de escritura en carpetas

- **Formato:** Comprobar que frontmatter YAML es válido

- **Transcripción:** Verificar campo `has_transcript` en archivo fuente



## Mantenimiento y Monitorización



### Logs a Revisar Regularmente

- `C:\\YT-Pipeline\\logs\\orchestrator.log`

- `C:\\AI-Broker\\logs\\broker.log`

- Logs de Ollama (ubicación según instalación)



### Métricas Importantes

- **Orchestrator:** Archivos procesados vs fallidos

- **Broker:** Tiempo medio de procesamiento por modelo

- **DeepSeek:** Uso mensual vs presupuesto configurado

- **Sistema:** Uso de VRAM y espacio en disco



### Actualizaciones

- **Modelos:** Actualizar Ollama y modelos regularmente

- **Código:** Aplicar updates de las aplicaciones

- **Configuración:** Revisar y ajustar perfiles de extracción



### Backup y Recuperación

- **Configuraciones:** Backup de `config.yaml` y `broker_config.yaml`

- **Datos:** Backup regular de carpeta Obsidian

- **Logs:** Rotación automática configurada para evitar llenar disco



## Escalado y Optimización



### Para Mayor Volumen

- **Concurrencia:** El MVP mantiene una sola tarea LLM activa; no aumentar este límite aunque crezca la cola

- **Hardware:** Añadir más VRAM o RAM según necesidad

- **Red:** Considerar conexión Gigabit entre máquinas



### Para Mejor Rendimiento

- **Modelos:** Usar modelos cuantizados (Q4, Q8) para balance velocidad/calidad

- **Caché:** Configurar `keep_alive` en Ollama para mantener modelos cargados

- **SSD:** Usar almacenamiento SSD para modelos y archivos temporales



Esta guía te permitirá tener el sistema completo funcionando en menos de 2 horas, asumiendo que ya tienes las máquinas y Ollama configurado básicamente.

## Despliegue Normativo del MVP

Esta sección prevalece sobre comandos o rutas anteriores.

### Directorios

En la máquina principal:

```powershell
$pipeline = "C:\YT-Pipeline"
$downloadInbox = Join-Path $env:USERPROFILE "Downloads\YT-Knowledge-Inbox"
New-Item -ItemType Directory -Force -Path $downloadInbox
New-Item -ItemType Directory -Force -Path "$pipeline\staging", "$pipeline\processing", "$pipeline\completed", "$pipeline\failed\contracts", "$pipeline\rejected\notes", "$pipeline\rejected\sources", "$pipeline\state", "$pipeline\logs"
```

El valor inicial de `paths.inbox` será `%USERPROFILE%/Downloads/YT-Knowledge-Inbox`. La UI permite cambiarlo para adaptarse a una carpeta Descargas personalizada.

En el Broker se crean `state/` y `logs/` junto a la aplicación. SQLite usa modo WAL; ambos directorios se incluyen en backups.

### Configuración corregida

```yaml
# Orchestrator
paths:
  inbox: "%USERPROFILE%/Downloads/YT-Knowledge-Inbox"
  staging: "C:/YT-Pipeline/staging"
  processing: "C:/YT-Pipeline/processing"
  completed: "C:/YT-Pipeline/completed"
  failed: "C:/YT-Pipeline/failed"
  rejected: "C:/YT-Pipeline/rejected"
  state: "C:/YT-Pipeline/state"

broker:
  hostname: "broker-machine.local"
  port: 8080
  poll_interval_seconds: 2
  retry_attempts: 3

processing:
  max_concurrent_ingestion: 2
  stable_file_checks: 3
  stable_file_interval_seconds: 1
  file_lock_retry_attempts: 3
  file_lock_retry_backoff_seconds: [1, 2, 4]
```

```yaml
# Broker
server:
  host: "192.168.1.50"   # IP de la interfaz LAN, no 0.0.0.0
  port: 8080
  workers: 1
  cors_enabled: false

persistence:
  database: "state/broker.db"
  journal_mode: "WAL"

processing:
  max_active_llm_tasks: 1
  queue_max_size: 1000
  task_timeout_seconds: 300
  unload_after_task: true

health:
  sqlite_interval_seconds: 10
  local_dependencies_interval_seconds: 30
  external_providers_interval_seconds: 300
  disk_free_alert_gb: 10
```

Las claves se almacenan con `keyring` en Windows Credential Manager. `.env` puede aportar una clave inicial que se migra y elimina del entorno operativo; nunca se persiste en SQLite o YAML. Los precios y presupuestos deben revisarse antes de activar un proveedor externo.

### Arranque y red

- Ejecutar el Broker con un worker Uvicorn. `--reload` se usa solo durante desarrollo.
- Instalarlo como servicio de Windows con inicio automático y recuperación tras fallo; el servicio no se considera listo hasta que `/health/ready` responda `200`.
- Permitir TCP 8080 exclusivamente desde la subred privada o desde la IP de la máquina principal.
- No configurar redirección de puertos en el router.
- El MVP mantiene la decisión de no usar autenticación; si el servicio sale de la LAN, TLS y autenticación pasan a ser obligatorios antes del despliegue.

### Pruebas de aceptación previas al uso

1. Capturar cuatro vídeos: subtítulos manuales, automáticos, múltiples idiomas y sin transcripción.
2. Verificar procesamiento automático desde Descargas sin movimiento manual.
3. Ingresar una transcripción no YouTube con metadata mínima.
4. Reiniciar ambas aplicaciones con tareas pendientes y activas; no deben aparecer duplicados.
5. Desconectar Ollama y el Broker, restaurarlos y comprobar recuperación.
6. Cancelar una tarea y reordenar pendientes desde el dashboard.
7. Rechazar una nota publicada y reprocesar su fuente.
8. Procesar contenido que requiera varios chunks.
9. Agotar de forma simulada el presupuesto externo y comprobar fallback o error controlado.
10. Ejecutar las suites unitarias, de contratos y end-to-end antes de empaquetar.
11. Encolar al menos tres tareas mientras la primera permanece generando; comprobar que solo la primera está `processing`, las otras siguen `queued` y la API continúa aceptando consultas y nuevas tareas.
12. Mantener un archivo bloqueado y verificar tres reintentos antes de `FILE_LOCKED`; después desbloquearlo y reprocesarlo manualmente.
13. Interrumpir el Orchestrator después del commit `STAGED` y antes/después de `os.replace`; ambos reinicios deben recuperar el fichero sin duplicarlo.
14. Enviar fixtures inválidos en cada frontera y verificar rechazo inmediato, mensaje de campo y ausencia de efectos parciales.
15. Verificar que al finalizar o cancelar una tarea se envía `keep_alive: 0`, `/api/ps` confirma la descarga y la siguiente tarea no comienza antes de liberar VRAM.
16. Detener Ollama y bloquear SQLite por separado; validar estados `degraded`/`unavailable`, readiness y recuperación automática.

### Empaquetado

- El plugin se entrega como carpeta Manifest V3 cargable sin empaquetar y ZIP reproducible.
- Orchestrator y Broker mantienen dependencias bloqueadas en sus respectivos lockfiles.
- El Orchestrator se empaqueta para Windows con consola deshabilitada, conservando configuración y SQLite fuera del ejecutable.
- El Broker se instala como aplicación Python nativa; Docker queda fuera del MVP.
