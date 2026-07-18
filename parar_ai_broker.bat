@echo off
setlocal

echo ==========================================
echo  AI Broker - Parada
echo ==========================================
echo.

set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  set "FOUND=1"
  echo Parando proceso en puerto 8765. PID: %%P
  taskkill /PID %%P /F
)

if not defined FOUND (
  echo No hay ningun servidor escuchando en el puerto 8765.
) else (
  echo.
  echo Listo. El puerto 8765 deberia haber quedado libre.
)

echo.
pause
