# Fase 6 — Endurecimiento operativo

## Estado actual

Primer bloque implementado: backup, verificacion y restore del estado durable del Broker.

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

## Pendiente de fase 6

- Rotacion y retencion de backups/logs.
- Instalacion como servicio Windows con reinicio automatico.
- Checklist de firewall LAN.
- Pruebas de SQLite read-only, disco lleno, Ollama caido, Credential Manager no disponible y readiness de clientes.
