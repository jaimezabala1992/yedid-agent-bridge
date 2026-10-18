@echo off
title Detener Yedid Smart Home Hub y MCP Gateway
cd /d "C:\Yedid-Smart-Home"

echo ========================================================
echo       DETENIENDO YEDID SMART HOME HUB Y MCP GATEWAY
echo ========================================================

powershell -NoProfile -Command "$ports = @(8765, 8766); foreach ($port in $ports) { $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue; if ($conns) { foreach ($c in $conns) { $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if ($proc -and $proc.ProcessName -like '*python*') { Write-Host \"Deteniendo proceso puerto $port PID: $($proc.Id)\"; Stop-Process -Id $proc.Id -Force } } } else { Write-Host \"Servicio en puerto $port no estaba en ejecucion.\" } }"

echo Yedid Hub y MCP Gateway detenidos limpiamente. Sunshine y demas servicios permanecen intactos.
timeout /t 3 >nul
