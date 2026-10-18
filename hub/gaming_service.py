"""
Servicio para la administración controlada de Sunshine Gaming Server y aplicaciones de entretenimiento autorizadas.
Lista blanca estricta: Sunshine, Steam, Moonlight.
Cero manipulación de procesos arbitrarios.
"""
import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
import psutil
from .audit import log_event

ALLOWED_GAMING_APPS = {
    "sunshine": {
        "name": "Sunshine Game Streaming",
        "process_names": ["sunshine.exe"],
        "service_name": "Sunshine",
        "allowed_actions": ["status", "start", "restart"]
    },
    "steam": {
        "name": "Steam Gaming Client",
        "process_names": ["steam.exe"],
        "path": Path("C:/Program Files (x86)/Steam/steam.exe"),
        "allowed_actions": ["status", "start"]
    },
    "moonlight": {
        "name": "Moonlight Game Streaming Client",
        "process_names": ["moonlight.exe"],
        "path": Path("C:/Program Files/Moonlight Game Streaming/Moonlight.exe"),
        "allowed_actions": ["status", "start"]
    }
}

class GamingService:
    def get_supported_apps(self) -> Dict[str, Any]:
        """Devuelve la lista blanca de aplicaciones y acciones permitidas."""
        return {
            k: {
                "name": v["name"],
                "allowed_actions": v["allowed_actions"]
            } for k, v in ALLOWED_GAMING_APPS.items()
        }

    async def manage_app(self, app_name: str, action: str) -> Dict[str, Any]:
        """Administra una aplicación de la lista blanca."""
        app_key = app_name.lower().strip()
        act_key = action.lower().strip()

        if app_key not in ALLOWED_GAMING_APPS:
            err = f"La aplicación '{app_name}' no está en la lista blanca autorizada."
            log_event("gaming_action_rejected", {"app": app_name, "action": action, "reason": "unauthorized_app"})
            return {"success": False, "error": err, "allowed_apps": list(ALLOWED_GAMING_APPS.keys())}

        app_config = ALLOWED_GAMING_APPS[app_key]
        if act_key not in app_config["allowed_actions"]:
            err = f"La acción '{action}' no está permitida para '{app_name}'."
            log_event("gaming_action_rejected", {"app": app_name, "action": action, "reason": "unauthorized_action"})
            return {"success": False, "error": err, "allowed_actions": app_config["allowed_actions"]}

        if act_key == "status":
            return await self._get_status(app_key, app_config)
        elif act_key == "start":
            return await self._start_app(app_key, app_config)
        elif act_key == "restart":
            return await self._restart_app(app_key, app_config)

        return {"success": False, "error": f"Acción '{act_key}' no manejada."}

    async def _get_status(self, app_key: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Comprueba si el proceso o servicio está corriendo."""
        running_pids = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pname = (proc.info['name'] or '').lower()
                if pname in [x.lower() for x in config["process_names"]]:
                    running_pids.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        is_running = len(running_pids) > 0
        log_event("gaming_status_check", {"app": app_key, "running": is_running, "pids": running_pids})
        return {
            "success": True,
            "app": app_key,
            "name": config["name"],
            "running": is_running,
            "instances_count": len(running_pids),
            "pids": running_pids,
            "gpu_dedicated": "AMD Radeon RX 6700 XT (Gaming Exclusivo)"
        }

    async def _start_app(self, app_key: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Inicia una aplicación si no está en ejecución."""
        status = await self._get_status(app_key, config)
        if status["running"]:
            return {
                "success": True,
                "app": app_key,
                "message": f"{config['name']} ya se encuentra en ejecución (PID: {status['pids'][0]}).",
                "running": True
            }

        app_path = config.get("path")
        if app_path and app_path.exists():
            subprocess.Popen([str(app_path)], close_fds=True)
            log_event("gaming_app_started", {"app": app_key, "path": str(app_path)})
            return {
                "success": True,
                "app": app_key,
                "message": f"Iniciando {config['name']} correctamente.",
                "running": True
            }

        # Para Sunshine si está registrado como servicio o ejecutable
        if app_key == "sunshine":
            try:
                subprocess.run(["powershell", "-Command", "Start-Service Sunshine -ErrorAction SilentlyContinue"], check=False)
                log_event("gaming_app_started", {"app": app_key, "method": "service"})
                return {"success": True, "app": app_key, "message": "Servicio Sunshine iniciado.", "running": True}
            except Exception as e:
                return {"success": False, "app": app_key, "error": str(e)}

        return {"success": False, "app": app_key, "error": "Ruta de ejecutable no encontrada."}

    async def _restart_app(self, app_key: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Reinicia Sunshine de forma segura."""
        if app_key != "sunshine":
            return {"success": False, "error": "El reinicio sólo está autorizado para el servicio Sunshine."}

        try:
            cmd = "Restart-Service Sunshine -ErrorAction SilentlyContinue"
            await asyncio.to_thread(subprocess.run, ["powershell", "-Command", cmd], check=False)
            log_event("gaming_service_restarted", {"service": "Sunshine"})
            return {
                "success": True,
                "app": "sunshine",
                "message": "Servicio Sunshine reiniciado correctamente."
            }
        except Exception as e:
            return {"success": False, "app": "sunshine", "error": str(e)}

gaming_service = GamingService()
