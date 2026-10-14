@echo off
echo Cerrando solo las instancias anteriores del bot...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'py.exe') -and $_.CommandLine -match 'bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
timeout /t 2 /nobreak >nul

echo Arrancando bot...
cd /d "%~dp0"
start /min py bot.py
echo Bot arrancado en segundo plano.
timeout /t 2 /nobreak >nul
