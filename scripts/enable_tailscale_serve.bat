@echo off
title Habilitar Tailscale Serve para Yedid Hub
cd /d "C:\Yedid-Smart-Home"

echo ========================================================
echo       HABILITAR TAILSCALE SERVE HTTPS (PUERTO 8443)
echo ========================================================
echo.
echo NOTA: Si es la primera vez que usas Serve en tu Tailnet,
echo asegurate de haber activado la funcion en:
echo https://login.tailscale.com/f/serve?node=nqox8RLSR421CNTRL
echo.
echo Configurando proxy HTTPS en la red privada Tailscale...
tailscale serve --bg --https=8443 --yes http://127.0.0.1:8765

echo.
echo Verificando estado:
tailscale serve status

echo.
echo URL de acceso privado desde tu Galaxy Z Fold 7:
echo https://yedid-hub.tailnet.ts.net:8443/
echo.
pause
