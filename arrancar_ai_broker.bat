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

echo Comprobando si el puerto 8080 ya esta en uso...
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if %ERRORLEVEL% EQU 0 (
  echo.
  echo Parece que ya hay algo escuchando en el puerto 8080.
  echo Si es AI Broker, abre:
  echo   http://127.0.0.1:8080/dashboard
  echo.
  echo Si quieres reiniciarlo, ejecuta primero parar_ai_broker.bat
  echo.
  pause
  exit /b 0
)

echo.
echo Arrancando AI Broker...
echo.
echo Panel:
echo   http://127.0.0.1:8080/dashboard
echo.
echo Para parar el servidor: pulsa Ctrl+C en esta ventana.
echo.

".venv\Scripts\python.exe" scripts\run_broker.py --config broker_config.yaml

echo.
echo AI Broker se ha detenido.
pause
