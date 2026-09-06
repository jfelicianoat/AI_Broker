@echo off
rem Arranca el broker sin ventana interactiva y deja la consola (incluido el
rem token de esta sesion) en logs\restart-console.log. Pensado para lanzarlo
rem desacoplado: la ventana de cmd no sobrevive a la sesion que la crea.
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\run_broker.py --config broker_config.yaml > "logs\restart-console.log" 2>&1
