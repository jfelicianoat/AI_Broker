# Fase 6 — Endurecimiento operativo

## Estado actual

Primer bloque implementado: backup, verificacion y restore del estado durable del Broker.

Segundo bloque implementado: logging operativo con rotacion.

Tercer bloque implementado: artefactos de despliegue como servicio Windows y checklist LAN/readiness.

El backup incluye:

- snapshot consistente de SQLite mediante la API `sqlite3.backup`;
- artefactos bajo `state/tasks`;
- `manifest.json` con formato, fecha, lista de archivos, tamaño y SHA-256;
- escritura atomica del zip final.

La restauracion:

- verifica manifest y checksums antes de escribir;
- valida SQLite con `PRAGMA integrity_check`;
- exige `--replace` para sobrescribir base o artefactos existentes;
- restaura DB y artefactos con `os.replace`.

## Comandos

```powershell
python scripts/backup_state.py backup --database state/broker.db --artifacts state/tasks --output backups/ai-broker-state.zip
python scripts/backup_state.py verify --backup backups/ai-broker-state.zip
python scripts/backup_state.py restore --backup backups/ai-broker-state.zip --database state/broker.db --artifacts state/tasks --replace
```

## Limites actuales

- No cifra backups.
- No sube backups a almacenamiento externo.
- No rota backups antiguos.
- No coordina parada del servicio Windows; para restaurar en produccion se debe detener el servicio antes del restore.
- No incluye `state/files` (ficheros ingeridos, fase 7): tras un restore, las filas `ingested_files` de la BD pueden apuntar a ficheros ausentes; re-subir el documento (dedupe por SHA-256) lo regenera.

## Logging operativo

La configuracion vive en `broker_config.yaml`:

```yaml
logging:
  level: "INFO"
  directory: "logs"
  filename: "ai-broker.log"
  max_bytes: 10485760
  backup_count: 5
  console_enabled: true
```

El formato es JSON Lines. El access log registra solo:

- metodo HTTP;
- ruta;
- codigo de estado;
- duracion en milisegundos;
- cliente.

No se registran cuerpos, prompts, respuestas, headers de autorizacion ni claves. Se silencian logs de access duplicados de `uvicorn.access` y ruido informativo de `httpx`.

## Retención de datos

Podas ejecutadas en el arranque, configurables en `persistence` (`0` = conservar siempre):

```yaml
persistence:
  events_retention_days: 30    # eventos de tareas terminales (prompt.compressed exento)
  artifacts_retention_days: 0  # artefactos en disco de tareas terminales
  files_retention_days: 0      # ficheros ingeridos ready/failed (converting jamás se poda)
```

Los ficheros ingeridos se podan con fila y directorio (`state/files/{id}/`). Una tarea encolada que referencie un fichero podado fallará en el despacho: retención corta con colas largas es mala combinación.

## Pendiente de fase 6

- Retencion avanzada de backups.
- Pruebas de SQLite read-only, disco lleno, Ollama caido, Credential Manager no disponible y readiness de clientes.

## Servicio Windows

Los scripts no instalan nada por si solos durante desarrollo; se ejecutan manualmente cuando se quiera desplegar.

Prerequisito recomendado: NSSM disponible en PATH o pasando `-Nssm C:\ruta\nssm.exe`.

```powershell
.\scripts\install_windows_service.ps1 -ServiceName "AI-Broker" -ProjectRoot "D:\Desarrollo\Proyectos TFM\AI_Broker"
Start-Service "AI-Broker"
python scripts/check_readiness.py --url http://127.0.0.1:8765/health/ready --timeout 60
```

Para retirar el servicio:

```powershell
.\scripts\uninstall_windows_service.ps1 -ServiceName "AI-Broker"
```

El runner de produccion es:

```powershell
python scripts/run_broker.py --config broker_config.yaml
```

Fija `workers=1` para preservar el invariante de un unico workflow Broker activo.

## Firewall LAN

Regla sugerida solo para perfil privado y `LocalSubnet`:

```powershell
.\scripts\configure_firewall_lan.ps1 -Port 8765 -WhatIf
.\scripts\configure_firewall_lan.ps1 -Port 8765
```

No exponer el Broker a Internet. Si se necesita acceso remoto, debe ir detras de VPN o tunel privado controlado.

## Checklist antes de arrancar clientes

1. `python scripts/check_readiness.py --url http://127.0.0.1:8765/health/ready --timeout 60`
2. Confirmar que `dependencies.sqlite.status = healthy`.
3. Confirmar que `/health/live` responde.
4. Confirmar que el puerto publicado coincide con `broker_config.yaml`.
5. Confirmar que el firewall esta limitado a LAN privada.
6. Confirmar que `logs/ai-broker.log` se escribe y rota.
7. Confirmar que existe un backup reciente verificado.
