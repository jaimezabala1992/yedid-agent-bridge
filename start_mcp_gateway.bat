@echo off
title Yedid MCP Gateway
cd /d "C:\Yedid-Smart-Home"

echo ========================================================
echo             INICIANDO YEDID MCP GATEWAY v1.0
echo ========================================================

:: Prevención de duplicados en el puerto 8766
powershell -NoProfile -Command "$conn = Get-NetTCPConnection -LocalPort 8766 -ErrorAction SilentlyContinue; if ($conn) { Write-Host '[!] Yedid MCP Gateway ya activo en el puerto 8766. Evitando duplicados.'; exit 100 }"
if %ERRORLEVEL% equ 100 (
    echo El servicio ya esta en ejecucion.
    exit /b 0
)

"%USERPROFILE%\Documents\Codex\runtime\python\python.exe" -m uvicorn mcp_gateway.server:gateway_app --host 127.0.0.1 --port 8766
