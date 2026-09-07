@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo  AI Broker - Token admin de esta sesion
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo No se encuentra el entorno virtual en "%~dp0.venv".
  echo.
  pause
  exit /b 1
)

rem El token se genera nuevo en cada arranque y el broker lo publica en el
rem Administrador de credenciales (servicio ai-broker, usuario
rem session_admin_token) porque cuando arranca desatendido nadie ve su consola.
"C:\Procesos\AI_Broker\.venv\Scripts\python.exe" -c "import keyring; t = keyring.get_password('ai-broker', 'session_admin_token'); print(t if t else 'No hay token publicado. Arranca el broker con publish_session_token: keyring en broker_config.yaml.')"

echo.
echo Cabecera para el API:  X-Admin-Token: ^<token^>
echo Panel:                 http://127.0.0.1:8765/dashboard
echo.
pause
