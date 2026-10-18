"""Administrador central de recursos y memoria para Yedid Smart Home.

Diseñado específicamente para optimizar el uso de RAM y VRAM en PCs de alto rendimiento
y gaming (AMD Ryzen 5 5600X, 16 GB RAM DDR4, AMD Radeon RX 6700 XT 12 GB con Sunshine/Moonlight).

Garantiza que:
1. Yedid Hub, MCP Gateway y Tailscale permanezcan siempre disponibles.
2. Ollama Qwen3 y Whisper faster-whisper se carguen estrictamente bajo demanda.
3. Se descarguen de forma automática tras periodos de inactividad configurables.
4. No se interrumpan tareas en ejecución ni se terminen procesos indiscriminadamente.
5. Permite alternar entre 3 perfiles: Equilibrado (default), Gaming y Respuesta Rápida.
"""
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
import psutil

from .config import BASE_DIR, DATA_DIR, OLLAMA_URL, OLLAMA_MODEL
from .audit import log_event

logger = logging.getLogger("yedid_resources")

RESOURCE_PROFILE_FILE = DATA_DIR / "resource_profile.json"

PROFILES = {
    "equilibrado": {
        "id": "equilibrado",
        "name": "Equilibrado",
        "description": "LLM 3 min, Whisper 1 min. Balance óptimo entre latencia de respuesta y ahorro de RAM.",
        "llm_keep_alive": "3m",
        "llm_idle_seconds": 180,
        "whisper_idle_seconds": 60,
        "aggressive_unload": False,
        "allow_preload": True
    },
    "gaming": {
        "id": "gaming",
        "name": "Gaming (Sunshine & Moonlight)",
        "description": "Máxima RAM y VRAM libre para juegos. Descarga rápida de modelos al terminar interacciones.",
        "llm_keep_alive": "30s",
        "llm_idle_seconds": 30,
        "whisper_idle_seconds": 30,
        "aggressive_unload": True,
        "allow_preload": False
    },
    "rapido": {
        "id": "rapido",
        "name": "Respuesta Rápida",
        "description": "LLM 10 min, Whisper 5 min. Modelos calientes en memoria para sesiones continuas de trabajo.",
        "llm_keep_alive": "10m",
        "llm_idle_seconds": 600,
        "whisper_idle_seconds": 300,
        "aggressive_unload": False,
        "allow_preload": True
    }
}

class ResourceManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._active_tasks: Dict[str, int] = {}
        self._last_used: Dict[str, float] = {}
        self._active_profile: str = "equilibrado"
        self._watchdog_task: Optional[asyncio.Task] = None
        self._load_saved_profile()

    def _load_saved_profile(self):
        """Carga el perfil persistido en disco si existe."""
        if RESOURCE_PROFILE_FILE.exists():
            try:
                data = json.loads(RESOURCE_PROFILE_FILE.read_text(encoding="utf-8"))
                p_id = data.get("active_profile", "equilibrado")
                if p_id in PROFILES:
                    self._active_profile = p_id
                    logger.info(f"Perfil de recursos cargado desde disco: {p_id}")
            except Exception as e:
                logger.warning(f"No se pudo leer perfil guardado: {e}")
                self._active_profile = "equilibrado"

    def _save_profile(self):
        """Persiste el perfil activo en disco."""
        try:
            RESOURCE_PROFILE_FILE.write_text(
                json.dumps({"active_profile": self._active_profile, "updated_at": time.time()}, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Error persistiendo perfil de recursos: {e}")

    def get_profile(self) -> Dict[str, Any]:
        """Obtiene la configuración del perfil activo."""
        return PROFILES.get(self._active_profile, PROFILES["equilibrado"])

    def set_profile(self, profile_id: str) -> Dict[str, Any]:
        """Cambia el perfil activo y actualiza parámetros de tiempo de vida."""
        clean_id = profile_id.lower().strip()
        if clean_id not in PROFILES:
            raise ValueError(f"Perfil desconocido: '{clean_id}'. Disponibles: {list(PROFILES.keys())}")

        self._active_profile = clean_id
        self._save_profile()
        profile_data = PROFILES[clean_id]

        log_event("resource_profile_changed", {
            "profile": clean_id,
            "llm_keep_alive": profile_data["llm_keep_alive"],
            "whisper_idle_seconds": profile_data["whisper_idle_seconds"]
        })

        # Si se cambia a modo gaming, programar chequeo inmediato para liberar modelos ociosos
        if profile_data.get("aggressive_unload"):
            asyncio.create_task(self.check_and_unload_idle_models())

        return profile_data

    def get_current_keep_alive(self) -> str:
        """Devuelve el valor string de keep_alive para Ollama (e.g. '3m', '30s', '10m')."""
        return self.get_profile().get("llm_keep_alive", "3m")

    def get_active_tasks_count(self, target: str = "") -> int:
        """Devuelve cuántas tareas activas hay en curso."""
        if target:
            return self._active_tasks.get(target, 0)
        return sum(self._active_tasks.values())

    @asynccontextmanager
    async def track_task(self, model_name: str):
        """Context manager asíncrono para rastrear tareas en curso.
        
        Garantiza que ningún modelo sea descargado mientras genera texto o ejecuta una tarea.
        """
        async with self._lock:
            self._active_tasks[model_name] = self._active_tasks.get(model_name, 0) + 1
            self._last_used[model_name] = time.time()

        try:
            yield
        finally:
            async with self._lock:
                self._active_tasks[model_name] = max(0, self._active_tasks.get(model_name, 1) - 1)
                self._last_used[model_name] = time.time()

            # En modo gaming, si terminaron todas las tareas del modelo, verificar descarga rápida
            if self.get_profile().get("aggressive_unload") and self._active_tasks.get(model_name, 0) == 0:
                asyncio.create_task(self._delayed_gaming_unload(model_name))

    async def _delayed_gaming_unload(self, model_name: str, delay_seconds: float = 20.0):
        """En perfil gaming, espera un breve lapso tras terminar la interacción y descarga si sigue ocioso."""
        await asyncio.sleep(delay_seconds)
        if self._active_tasks.get(model_name, 0) == 0:
            logger.info(f"[Gaming Profile] Descargando modelo ocioso {model_name} para optimizar juegos.")
            await self.unload_ollama_model(model_name)

    async def get_loaded_ollama_models(self) -> List[Dict[str, Any]]:
        """Consulta qué modelos están realmente cargados en RAM/VRAM en Ollama."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{OLLAMA_URL}/api/ps")
                if res.status_code == 200:
                    data = res.json()
                    models = []
                    now = time.time()
                    for m in data.get("models", []):
                        name = m.get("name", m.get("model", "desconocido"))
                        size_bytes = m.get("size", 0)
                        size_vram = m.get("size_vram", 0)
                        expires_at = m.get("expires_at", "")
                        
                        models.append({
                            "name": name,
                            "size_bytes": size_bytes,
                            "size_mb": round(size_bytes / (1024 * 1024), 1),
                            "size_vram_mb": round(size_vram / (1024 * 1024), 1),
                            "expires_at": expires_at,
                            "active_tasks": self._active_tasks.get(name, 0),
                            "last_used_seconds_ago": round(now - self._last_used.get(name, now), 1) if name in self._last_used else None
                        })
                    return models
        except Exception as e:
            logger.warning(f"Error consultando Ollama /api/ps: {e}")
        return []

    async def unload_ollama_model(self, model_name: str, force: bool = False) -> bool:
        """Descarga un modelo de Ollama inmediatamente pasando keep_alive=0."""
        # Protección estricta: nunca descargar si hay tareas activas a menos que sea forzado
        if not force and self._active_tasks.get(model_name, 0) > 0:
            logger.warning(f"No se puede descargar '{model_name}': tiene {self._active_tasks[model_name]} tarea(s) en curso.")
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": model_name, "keep_alive": 0}
                )
                if res.status_code == 200:
                    log_event("ollama_model_unloaded", {"model": model_name, "forced": force})
                    logger.info(f"Modelo Ollama '{model_name}' descargado exitosamente (RAM/VRAM liberada).")
                    return True
        except Exception as e:
            logger.error(f"Error solicitando descarga de modelo '{model_name}': {e}")
        return False

    async def unload_all_idle_models(self) -> Dict[str, Any]:
        """Descarga todos los modelos de Ollama y el worker de Whisper que estén inactivos."""
        from .whisper_service import whisper_manager

        results = {
            "ollama_unloaded": [],
            "whisper_unloaded": False,
            "memory_freed": True
        }

        # 1. Descargar modelos de Ollama ociosos
        loaded = await self.get_loaded_ollama_models()
        for m in loaded:
            m_name = m["name"]
            if self._active_tasks.get(m_name, 0) == 0:
                ok = await self.unload_ollama_model(m_name)
                if ok:
                    results["ollama_unloaded"].append(m_name)

        # 2. Descargar worker de Whisper si está vivo y sin tareas
        if self._active_tasks.get("whisper", 0) == 0:
            if whisper_manager.is_worker_alive():
                unloaded = whisper_manager.unload_worker()
                results["whisper_unloaded"] = unloaded

        return results

    async def check_and_unload_idle_models(self):
        """Chequeo periódico ejecutado por el perro guardián (watchdog)."""
        from .whisper_service import whisper_manager

        profile = self.get_profile()
        llm_idle_max = profile.get("llm_idle_seconds", 180)
        whisper_idle_max = profile.get("whisper_idle_seconds", 60)
        now = time.time()

        # 1. Comprobar Whisper
        if whisper_manager.is_worker_alive() and self._active_tasks.get("whisper", 0) == 0:
            idle_w = whisper_manager.get_idle_seconds()
            if idle_w >= whisper_idle_max:
                logger.info(f"Whisper ha estado inactivo durante {idle_w:.1f}s (max: {whisper_idle_max}s). Descargando worker...")
                whisper_manager.unload_worker()

        # 2. Comprobar Ollama
        loaded = await self.get_loaded_ollama_models()
        for m in loaded:
            m_name = m["name"]
            if self._active_tasks.get(m_name, 0) == 0:
                last_used = self._last_used.get(m_name, now)
                idle_llm = now - last_used
                if idle_llm >= llm_idle_max:
                    logger.info(f"Modelo Ollama '{m_name}' inactivo durante {idle_llm:.1f}s. Descargando...")
                    await self.unload_ollama_model(m_name)

    async def start_watchdog(self):
        """Inicia el bucle de monitoreo continuo en segundo plano."""
        if self._watchdog_task and not self._watchdog_task.done():
            return

        async def _loop():
            logger.info("Perro guardián (Resource Watchdog) de Yedid iniciado.")
            while True:
                try:
                    await asyncio.sleep(10.0)
                    await self.check_and_unload_idle_models()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error en watchdog de recursos: {e}")

        self._watchdog_task = asyncio.create_task(_loop())

    def stop_watchdog(self):
        """Detiene el perro guardián."""
        if self._watchdog_task:
            self._watchdog_task.cancel()

    async def get_resource_status(self) -> Dict[str, Any]:
        """Obtiene una radiografía completa de memoria, modelos y estado de carga."""
        from .whisper_service import whisper_manager

        loaded_models = await self.get_loaded_ollama_models()
        vm = psutil.virtual_memory()

        whisper_alive = whisper_manager.is_worker_alive()
        whisper_ram = whisper_manager.get_worker_memory_mb() if whisper_alive else 0.0

        return {
            "active_profile": self._active_profile,
            "profile_details": self.get_profile(),
            "all_profiles": PROFILES,
            "active_tasks_total": sum(self._active_tasks.values()),
            "active_tasks_breakdown": dict(self._active_tasks),
            "system_ram": {
                "total_gb": round(vm.total / (1024**3), 2),
                "used_gb": round(vm.used / (1024**3), 2),
                "free_gb": round(vm.available / (1024**3), 2),
                "percent_used": vm.percent
            },
            "llm": {
                "loaded_models_count": len(loaded_models),
                "models": loaded_models,
                "current_keep_alive": self.get_current_keep_alive()
            },
            "whisper": {
                "is_loaded": whisper_alive,
                "pid": whisper_manager.get_worker_pid() if whisper_alive else None,
                "ram_mb": whisper_ram,
                "idle_seconds": whisper_manager.get_idle_seconds() if whisper_alive else None,
                "timeout_seconds": self.get_profile().get("whisper_idle_seconds", 60)
            }
        }

resource_manager = ResourceManager()
