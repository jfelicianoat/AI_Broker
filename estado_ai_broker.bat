@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo  AI Broker - Estado
echo ==========================================
echo.

rem El host real vive en broker_config.yaml (server.host); leerlo evita que
rem este script se quede anticuado si cambia (p. ej. de 127.0.0.1 a la IP LAN).
set "BROKER_HOST=127.0.0.1"
for /f "usebackq tokens=2 delims=: " %%H in (`findstr /r /c:"^  host:" broker_config.yaml`) do set "BROKER_HOST=%%H"

echo Puerto 8765:
netstat -ano | findstr ":8765" | findstr "LISTENING"
if %ERRORLEVEL% NEQ 0 (
  echo No hay nada escuchando en el puerto 8765.
  echo.
  pause
  exit /b 0
)

echo.
echo Probando /health en %BROKER_HOST%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -Uri 'http://%BROKER_HOST%:8765/health' | ConvertTo-Json -Depth 6 } catch { Write-Host 'No se pudo consultar /health:' $_.Exception.Message; exit 1 }"

echo.
echo Panel:
echo   http://%BROKER_HOST%:8765/dashboard
echo.
pause
