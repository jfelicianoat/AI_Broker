@echo off
rem Activa la auditoria de creacion y cierre de procesos (eventos 4688 y 4689
rem del registro de Seguridad), con la linea de comandos incluida en el 4688.
rem Sirve para averiguar quien mata python.exe si el broker vuelve a caer.
rem Hay que ejecutarlo como administrador (clic derecho > Ejecutar como administrador).
net session >nul 2>&1 || (echo Ejecutalo como administrador. & pause & exit /b 1)
auditpol /set /subcategory:"{0CCE922B-69AE-11D9-BED3-505054503030}" /success:enable
auditpol /set /subcategory:"{0CCE922C-69AE-11D9-BED3-505054503030}" /success:enable
reg add "HKLM\Software\Microsoft\Windows\CurrentVersion\Policies\System\Audit" /v ProcessCreationIncludeCmdLine_Enabled /t REG_DWORD /d 1 /f
rem Agranda el registro de Seguridad a 256 MB para que no se recicle en horas.
wevtutil sl Security /ms:268435456
echo.
auditpol /get /subcategory:"{0CCE922B-69AE-11D9-BED3-505054503030}"
auditpol /get /subcategory:"{0CCE922C-69AE-11D9-BED3-505054503030}"
pause
