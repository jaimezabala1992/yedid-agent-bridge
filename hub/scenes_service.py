"""Servicio de Escenas y Rutinas Domóticas de 1-Clic para Yedid Hub."""
import asyncio
from typing import Any, Dict
from .tv_service import tv_service
from .system_service import get_system_vitals
from .audit import log_event

SCENES_CATALOG = {
    "modo_cine": {
        "title": "Modo Cine 🍿",
        "description": "Enciende el televisor de la sala y abre Netflix para maratón de películas.",
        "icon": "🎬"
    },
    "modo_gaming": {
        "title": "Modo Gaming 🎮",
        "description": "Verifica Sunshine en PC, prepara el stream y enciende la TV.",
        "icon": "⚡"
    },
    "modo_musica": {
        "title": "Modo Música 🎵",
        "description": "Enciende el televisor de la sala y abre Spotify para ambientar.",
        "icon": "🎶"
    },
    "modo_noche": {
        "title": "Modo Buenas Noches 🌙",
        "description": "Apaga el televisor de la sala y comprueba la PC para descanso.",
        "icon": "🛌"
    }
}

class ScenesService:
    async def execute_scene(self, scene_name: str) -> Dict[str, Any]:
        """Ejecuta una macro orquestada con pasos seguros y secuenciales."""
        name = scene_name.strip().lower()
        if name not in SCENES_CATALOG:
            return {
                "success": False,
                "error": f"Escena '{scene_name}' no reconocida. Disponibles: {list(SCENES_CATALOG.keys())}"
            }

        log_event("scene_execution_start", {"scene": name})

        try:
            steps = []
            if name == "modo_cine":
                # 1. Asegurar TV encendida
                pwr_res = await tv_service.set_power(power_on=True)
                steps.append({"step": "encender_tv", "success": pwr_res.get("success", False), "detail": pwr_res})
                await asyncio.sleep(1.5)

                # 2. Abrir Netflix
                launch_res = await tv_service.launch_app("netflix")
                steps.append({"step": "lanzar_netflix", "success": launch_res.get("success", False), "detail": launch_res})

                # 3. Telemetría de PC
                vitals = await asyncio.to_thread(get_system_vitals)
                steps.append({"step": "verificar_pc", "success": True, "detail": vitals.get("status")})

                scene_ok = all(s.get("success", False) for s in steps)
                res = {
                    "success": scene_ok,
                    "scene": name,
                    "steps": steps,
                    "pasos_completados": len(steps),
                    "message": "Modo Cine activado: Televisor encendido y Netflix abierto." if scene_ok else "Modo Cine completado con advertencias en pasos.",
                    "tv_app": launch_res.get("current_app"),
                    "pc_status": vitals.get("status")
                }
                log_event("scene_execution_success" if scene_ok else "scene_execution_partial", res)
                return res

            elif name == "modo_gaming":
                # 1. Verificar Sunshine en la PC
                vitals = await asyncio.to_thread(get_system_vitals)
                sunshine_ok = vitals.get("sunshine_gaming_server") == "Activo"
                steps.append({"step": "verificar_sunshine", "success": sunshine_ok, "detail": vitals.get("sunshine_gaming_server")})

                # 2. Asegurar TV encendida
                pwr_res = await tv_service.set_power(power_on=True)
                steps.append({"step": "encender_tv", "success": pwr_res.get("success", False), "detail": pwr_res})
                await asyncio.sleep(1.0)

                scene_ok = all(s.get("success", False) for s in steps)
                res = {
                    "success": scene_ok,
                    "scene": name,
                    "steps": steps,
                    "pasos_completados": len(steps),
                    "message": f"Modo Gaming listo: Sunshine está {vitals.get('sunshine_gaming_server')}, CPU al {vitals.get('cpu_percent')}%, RAM libre {vitals.get('ram', {}).get('free_gb')} GB. Televisor listo.",
                    "sunshine_active": sunshine_ok,
                    "vitals": vitals
                }
                log_event("scene_execution_success" if scene_ok else "scene_execution_partial", res)
                return res

            elif name == "modo_musica":
                # 1. Asegurar TV encendida
                pwr_res = await tv_service.set_power(power_on=True)
                steps.append({"step": "encender_tv", "success": pwr_res.get("success", False), "detail": pwr_res})
                await asyncio.sleep(1.5)

                # 2. Lanzar Spotify en Google TV
                launch_res = await tv_service.launch_app("spotify")
                steps.append({"step": "lanzar_spotify", "success": launch_res.get("success", False), "detail": launch_res})

                scene_ok = all(s.get("success", False) for s in steps)
                res = {
                    "success": scene_ok,
                    "scene": name,
                    "steps": steps,
                    "pasos_completados": len(steps),
                    "message": "Modo Música activado: Televisor encendido con Spotify en reproducción." if scene_ok else "Modo Música ejecutado con advertencias en el televisor.",
                    "tv_app": launch_res.get("current_app")
                }
                log_event("scene_execution_success" if scene_ok else "scene_execution_partial", res)
                return res

            elif name == "modo_noche":
                # 1. Apagar televisor de la sala
                pwr_res = await tv_service.set_power(power_on=False)
                steps.append({"step": "apagar_tv", "success": pwr_res.get("success", False), "detail": pwr_res})

                # 2. Estado de la PC
                vitals = await asyncio.to_thread(get_system_vitals)
                steps.append({"step": "verificar_pc", "success": True, "detail": vitals.get("status")})

                scene_ok = all(s.get("success", False) for s in steps)
                res = {
                    "success": scene_ok,
                    "scene": name,
                    "steps": steps,
                    "pasos_completados": len(steps),
                    "message": "Modo Buenas Noches ejecutado: Televisor de la sala apagado. PC en estado óptimo." if scene_ok else "Modo Noche ejecutado con advertencias.",
                    "tv_power": pwr_res
                }
                log_event("scene_execution_success" if scene_ok else "scene_execution_partial", res)
                return res

        except Exception as e:
            err = {"success": False, "scene": name, "steps": steps if 'steps' in locals() else [], "error": str(e)}
            log_event("scene_execution_error", err)
            return err

scenes_service = ScenesService()
