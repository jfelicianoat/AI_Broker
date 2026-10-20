@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo  AI Broker - Activar HTTPS
echo ==========================================
echo.
rem Antes: el PC principal emite el certificado con la CA del broker y deja
rem ca.crt, server.crt y server.key en C:\Agora\broker-https de este PC.

if not exist "C:\Agora\broker-https\server.crt" goto :faltan
if not exist "C:\Agora\broker-https\server.key" goto :faltan
if not exist "C:\Agora\broker-https\ca.crt" goto :faltan

".venv\Scripts\python.exe" scripts\activar_https.py
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo No se ha cambiado nada en el broker.
  pause
  exit /b 1
)

rem La clave privada solo la lee el usuario que corre el broker.
icacls "certs\server.key" /inheritance:r /grant:r "%USERNAME%:R" >nul

echo.
echo Reiniciando el broker...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do taskkill /PID %%P /F >nul 2>&1
timeout /t 3 /nobreak >nul
start "AI Broker" /min cmd /c arrancar_ai_broker_desatendido.bat
timeout /t 15 /nobreak >nul

".venv\Scripts\python.exe" -c "import httpx; ca = r'certs\broker-ca.crt'; print('HTTPS en la red:', httpx.get('https://192.168.1.52:8765/health', verify=ca).status_code); print('HTTPS en loopback:', httpx.get('https://127.0.0.1:8765/health', verify=ca).status_code)"
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo El broker no contesta como se esperaba. Mira logs\restart-console.log
)
echo.
pause
exit /b 0

:faltan
echo Faltan ca.crt, server.crt o server.key en C:\Agora\broker-https.
echo Los emite el PC principal con la CA del broker.
echo.
pause
exit /b 1
