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

rem Con server.tls_* el broker habla HTTPS, tambien en loopback, con el
rem certificado de la CA del broker (certs\broker-ca.crt).
set "BROKER_SCHEME=http"
findstr /r /c:"^  tls_certfile:" broker_config.yaml >nul && set "BROKER_SCHEME=https"
if "%BROKER_HOST%"=="0.0.0.0" set "BROKER_HOST=127.0.0.1"

echo.
echo Probando /health en 127.0.0.1...
".venv\Scripts\python.exe" -c "import json, os, httpx; ca = r'certs\broker-ca.crt'; r = httpx.get('%BROKER_SCHEME%://127.0.0.1:8765/health', verify=ca if os.path.isfile(ca) else True, timeout=10); print(json.dumps(r.json(), indent=2, ensure_ascii=True))"
if %ERRORLEVEL% NEQ 0 echo No se pudo consultar /health.

echo.
echo Panel:
echo   %BROKER_SCHEME%://%BROKER_HOST%:8765/dashboard
echo.
pause
