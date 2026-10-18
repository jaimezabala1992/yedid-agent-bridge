"""Servicio de control para ONN Google TV mediante androidtvremote2."""
import asyncio
from pathlib import Path
from typing import Any, Dict
from androidtvremote2 import AndroidTVRemote
from .config import TV_HOST, TV_CERT_FILE, TV_KEY_FILE
from .audit import log_event

APP_SHORTCUTS = {
    "youtube": "com.google.android.youtube.tv",
    "netflix": "com.netflix.ninja",
    "prime": "com.amazon.amazonvideo.livingroom",
    "prime video": "com.amazon.amazonvideo.livingroom",
    "disney": "com.disney.disneyplus",
    "disney+": "com.disney.disneyplus",
    "max": "com.wbd.stream",
    "hbo": "com.wbd.stream",
    "tailscale": "com.tailscale.ipn",
    "spotify": "com.spotify.tv.android",
    "twitch": "tv.twitch.android.app",
    "plex": "com.plexapp.android",
    "home": "com.google.android.apps.tv.launcherx",
    "launcher": "com.google.android.apps.tv.launcherx"
}

ALLOWED_KEYS = {
    "POWER", "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE", "MUTE",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "BACK", "HOME", "PLAY", "PAUSE", "PLAY_PAUSE", "WAKEUP", "SLEEP"
}

class TVService:
    def __init__(self, host: str = TV_HOST, cert_file: Path = TV_CERT_FILE, key_file: Path = TV_KEY_FILE):
        self.host = host
        self.cert_file = str(cert_file)
        self.key_file = str(key_file)

    def _create_remote(self) -> AndroidTVRemote:
        return AndroidTVRemote(
            client_name="Yedid Hub",
            certfile=self.cert_file,
            keyfile=self.key_file,
            host=self.host,
            enable_ime=False,
        )

    async def get_status(self, timeout: float = 4.0) -> Dict[str, Any]:
        """Obtiene el estado actual de encendido y aplicación de la TV."""
        remote = self._create_remote()
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=timeout)
            await asyncio.sleep(0.4)
            is_on = bool(remote.is_on)
            app = remote.current_app or ("Encendida (Menú principal)" if is_on else "En reposo / Apagada")
            remote.disconnect()
            res = {"success": True, "is_on": is_on, "current_app": app, "host": self.host}
            log_event("tv_status_check", res)
            return res
        except asyncio.TimeoutError:
            res = {"success": False, "is_on": False, "error": f"Timeout al conectar con TV en {self.host}"}
            log_event("tv_status_timeout", res)
            return res
        except Exception as e:
            res = {"success": False, "is_on": False, "error": str(e)}
            log_event("tv_status_error", res)
            return res

    async def send_key(self, key_name: str, timeout: float = 4.0) -> Dict[str, Any]:
        """Envía una tecla de control permitida a la TV."""
        upper_key = key_name.strip().upper()
        if upper_key not in ALLOWED_KEYS:
            return {"success": False, "error": f"Tecla no permitida: {key_name}. Permitidas: {sorted(list(ALLOWED_KEYS))}"}

        remote = self._create_remote()
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=timeout)
            await asyncio.sleep(0.3)
            remote.send_key_command(upper_key)
            await asyncio.sleep(0.5)
            is_on = bool(remote.is_on)
            remote.disconnect()
            res = {"success": True, "action": "send_key", "key": upper_key, "is_on": is_on}
            log_event("tv_key_command", res)
            return res
        except Exception as e:
            res = {"success": False, "error": str(e)}
            log_event("tv_key_error", res)
            return res

    async def set_power(self, power_on: bool) -> Dict[str, Any]:
        """Enciende o apaga la TV según el estado deseado."""
        status = await self.get_status()
        current_state = status.get("is_on", False)

        if power_on and current_state:
            return {"success": True, "message": "El televisor ya se encuentra encendido", "is_on": True}
        if not power_on and not current_state and status.get("success"):
            return {"success": True, "message": "El televisor ya se encuentra apagado o en reposo", "is_on": False}

        # Enviar comando de encendido/apagado
        key = "POWER"
        return await self.send_key(key)

    async def launch_app(self, app_name_or_package: str, timeout: float = 6.0) -> Dict[str, Any]:
        """Abre una aplicación específica en la TV."""
        raw_name = app_name_or_package.strip().lower()
        package = APP_SHORTCUTS.get(raw_name, app_name_or_package.strip())

        remote = self._create_remote()
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=timeout)
            await asyncio.sleep(0.3)
            # Si la TV está apagada, enviamos comando de encendido primero
            if not remote.is_on:
                remote.send_key_command("POWER")
                await asyncio.sleep(2.0)

            remote.send_launch_app_command(package)
            await asyncio.sleep(1.5)
            curr = remote.current_app
            remote.disconnect()
            res = {"success": True, "app_requested": raw_name, "package": package, "current_app": curr}
            log_event("tv_launch_app", res)
            return res
        except Exception as e:
            res = {"success": False, "error": str(e)}
            log_event("tv_launch_error", res)
            return res

tv_service = TVService()
