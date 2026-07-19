@echo off
setlocal

echo ==========================================
echo  AI Broker - Estado
echo ==========================================
echo.

echo Puerto 8765:
netstat -ano | findstr ":8765" | findstr "LISTENING"
if %ERRORLEVEL% NEQ 0 (
  echo No hay nada escuchando en el puerto 8765.
  echo.
  pause
  exit /b 0
)

echo.
echo Probando /health...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' | ConvertTo-Json -Depth 6 } catch { Write-Host 'No se pudo consultar /health:' $_.Exception.Message; exit 1 }"

echo.
echo Panel:
echo   http://127.0.0.1:8765/dashboard
echo.
pause
