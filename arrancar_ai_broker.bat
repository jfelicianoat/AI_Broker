@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo  AI Broker - Arranque
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo No encuentro .venv\Scripts\python.exe
  echo.
  echo Ejecuta primero la instalacion del entorno, o revisa que estes en la carpeta correcta:
  echo %CD%
  echo.
  pause
  exit /b 1
)

rem El venv guarda rutas absolutas del Python con el que se creo: al mover el
rem proyecto a otro PC (o mover Python) queda roto aunque el .exe exista.
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo El entorno .venv esta roto o se creo en otro PC.
  echo Recrealo desde esta carpeta con:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -e .[dev]
  echo.
  pause
  exit /b 1
)

echo Comprobando si el puerto 8765 ya esta en uso...
set "PUERTO_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do set "PUERTO_PID=%%P"
if defined PUERTO_PID (
  echo.
  echo Ya hay algo escuchando en el puerto 8765. Proceso propietario:
  echo.
  tasklist /fi "PID eq %PUERTO_PID%"
  echo.
  echo Si es AI Broker ^(python.exe^), el panel ya esta disponible en la URL
  echo que mostro su ventana de arranque ^(http://^<server.host^>:8765/dashboard,
  echo con el host definido en broker_config.yaml^).
  echo   Para reiniciarlo, ejecuta primero parar_ai_broker.bat
  echo.
  echo Si es OTRO programa, cierralo o cambia server.port en broker_config.yaml
  echo antes de arrancar: dos servicios en el mismo puerto provocan respuestas
  echo de un servidor que no es AI Broker.
  echo.
  pause
  exit /b 0
)

echo.
echo Arrancando AI Broker...
echo La URL del panel y el token de esta sesion se muestran a continuacion.
echo.
echo Para parar el servidor: pulsa Ctrl+C en esta ventana.
echo.

".venv\Scripts\python.exe" scripts\run_broker.py --config broker_config.yaml

echo.
echo AI Broker se ha detenido.
pause
