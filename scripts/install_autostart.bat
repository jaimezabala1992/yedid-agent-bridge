@echo off
title Configurar Inicio Automatico Yedid Hub
echo Creando acceso directo en la carpeta Inicio de Windows...
powershell -NoProfile -Command "$wsh = New-Object -ComObject WScript.Shell; $startup = [Environment]::GetFolderPath('Startup'); $sc = $wsh.CreateShortcut(\"$startup\Yedid Hub Silencioso.lnk\"); $sc.TargetPath = 'wscript.exe'; $sc.Arguments = '\"C:\Yedid-Smart-Home\scripts\start_silent.vbs\"'; $sc.WorkingDirectory = 'C:\Yedid-Smart-Home'; $sc.Save(); Write-Host 'Acceso directo de inicio automatico creado con exito en:' $startup"
pause
