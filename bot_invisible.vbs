Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
' Cierra solo las instancias anteriores del bot (no el resto de procesos Python del equipo)
WshShell.Run "powershell -NoProfile -WindowStyle Hidden -Command ""Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'py.exe') -and $_.CommandLine -match 'bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }""", 0, True
WScript.Sleep 2000
WshShell.Run "cmd /c cd /d ""C:\Users\PORTATIL OFICINA\Downloads\Partes de trabajo"" && py bot.py", 0, False
Set WshShell = Nothing
