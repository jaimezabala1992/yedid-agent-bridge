@echo off
title Yedid Smart Home Hub
cd /d "C:\Yedid-Smart-Home"

echo ========================================================
echo          INICIANDO YEDID SMART HOME HUB v2.0
echo ========================================================

:: 1. Prevención de procesos duplicados en el puerto 8765
powershell -NoProfile -Command "$conn = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue; if ($conn) { Write-Host '[!] Yedid Hub ya se encuentra activo en el puerto 8765. Evitando duplicados.'; exit 100 }"
if %ERRORLEVEL% equ 100 (
    echo El servicio ya esta en ejecucion.
    exit /b 0
)

:: 2. Verificación y configuración de Ollama con optimización de RAM
echo [1/3] Verificando servidor Ollama local (limite de 1 modelo y descarga automatica)...
set OLLAMA_KEEP_ALIVE=3m
set OLLAMA_MAX_LOADED_MODELS=1
set OLLAMA_NUM_PARALLEL=1
powershell -NoProfile -Command "$env:OLLAMA_KEEP_ALIVE='3m'; $env:OLLAMA_MAX_LOADED_MODELS='1'; $env:OLLAMA_NUM_PARALLEL='1'; if (-not (Get-Process -Name '*ollama*' -ErrorAction SilentlyContinue)) { Write-Host 'Iniciando Ollama daemon optimizado...'; Start-Process '%USERPROFILE%\AppData\Local\Programs\Ollama\ollama.exe' -ArgumentList 'serve' -WindowStyle Hidden }"

:: 3. Verificación de Yedid MCP Gateway en 8766
echo [2/3] Verificando Yedid MCP Gateway en http://127.0.0.1:8766...
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 8766 -ErrorAction SilentlyContinue)) { Write-Host 'Iniciando Yedid MCP Gateway...'; Start-Process -FilePath '%USERPROFILE%\Documents\Codex\runtime\python\python.exe' -ArgumentList '-m uvicorn mcp_gateway.server:gateway_app --host 127.0.0.1 --port 8766' -WorkingDirectory 'C:\Yedid-Smart-Home' -WindowStyle Hidden }"

:: 4. Inicio del Hub FastAPI
echo [3/3] Levantando servidor Yedid Hub en http://127.0.0.1:8765...
"%USERPROFILE%\Documents\Codex\runtime\python\python.exe" -m uvicorn hub.server:app --host 127.0.0.1 --port 8765
