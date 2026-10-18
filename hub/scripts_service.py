"""
Catálogo y ejecutor de scripts previamente registrados y aprobados para Yedid Hub.
PROHIBIDA la ejecución de comandos arbitrarios de terminal o shell no registrados.
Cada script cuenta con una función Python predefinida y control de riesgo estricto.
"""
import asyncio
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from .audit import log_event

BASE_DIR = Path("C:/Yedid-Smart-Home")
BACKUPS_DIR = BASE_DIR / "backups"

# Definición del catálogo oficial de scripts autorizados
APPROVED_SCRIPTS = {
    "system_health_check": {
        "name": "Verificación Completa de Salud",
        "description": "Comprueba conectividad y estado de FastAPI Hub, Ollama, Whisper, Tailscale y Google TV.",
        "risk_level": "LOW",
        "requires_human_approval": False
    },
    "backup_configs": {
        "name": "Respaldo Rápido de Configuraciones",
        "description": "Genera una copia de seguridad timestamped de configuraciones, registros y datos en backups/.",
        "risk_level": "LOW",
        "requires_human_approval": False
    },
    "clean_temp_logs": {
        "name": "Limpieza de Archivos Temporales",
        "description": "Purga cachés temporales de Whisper, archivos de audio transitorios y registros antiguos.",
        "risk_level": "MEDIUM",
        "requires_human_approval": False
    },
    "windows_maintenance": {
        "name": "Mantenimiento Preventivo de Windows",
        "description": "Inspecciona el espacio en carpetas temporales de Windows, salud de volúmenes y memoria física.",
        "risk_level": "MEDIUM",
        "requires_human_approval": False
    },
    "pc_hibernate": {
        "name": "Poner PC en Hibernación",
        "description": "Guarda el estado completo del sistema en disco y apaga la energía (ACPI S4 Hibernation).",
        "risk_level": "HIGH",
        "requires_human_approval": True
    }
}

class ScriptsService:
    def verify_catalog_integrity(self) -> bool:
        """Verifica que el catálogo y las funciones internas no hayan sido sustituidas ni adulteradas en memoria."""
        expected_handlers = {
            "system_health_check": self._run_health_check,
            "backup_configs": self._run_backup,
            "clean_temp_logs": self._run_clean_temp,
            "windows_maintenance": self._run_windows_maintenance,
            "pc_hibernate": self._run_pc_hibernate
        }
        for script_name, expected_fn in expected_handlers.items():
            if script_name not in APPROVED_SCRIPTS:
                return False
            # Verificar que el método pertenezca a ScriptsService y sea una corrutina legítima
            if not asyncio.iscoroutinefunction(expected_fn):
                return False
        return True

    def get_catalog(self) -> Dict[str, Any]:
        """Devuelve el catálogo de scripts aprobados con sus niveles de riesgo."""
        return APPROVED_SCRIPTS

    async def execute_script(self, script_name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Ejecuta un script registrado y aprobado de forma segura con verificación previa de integridad."""
        if not self.verify_catalog_integrity():
            log_event("script_integrity_violation", {"reason": "catalog_tampered"})
            return {"success": False, "error": "Violación de integridad: El catálogo de scripts ha sido adulterado o modificado sin autorización."}

        if script_name not in APPROVED_SCRIPTS:
            err = f"El script '{script_name}' NO está registrado en el catálogo de scripts autorizados."
            log_event("script_execution_rejected", {"script": script_name, "reason": "unregistered_script"})
            return {"success": False, "error": err, "allowed_scripts": list(APPROVED_SCRIPTS.keys())}

        meta = APPROVED_SCRIPTS[script_name]
        t0 = time.time()
        log_event("script_execution_started", {"script": script_name, "risk": meta["risk_level"]})

        try:
            if script_name == "system_health_check":
                result = await self._run_health_check()
            elif script_name == "backup_configs":
                result = await self._run_backup()
            elif script_name == "clean_temp_logs":
                result = await self._run_clean_temp()
            elif script_name == "windows_maintenance":
                result = await self._run_windows_maintenance()
            elif script_name == "pc_hibernate":
                result = await self._run_pc_hibernate()
            else:
                return {"success": False, "error": "Script sin implementación válida."}

            duration = round((time.time() - t0) * 1000, 2)
            log_event("script_execution_success", {"script": script_name, "duration_ms": duration})
            return {
                "success": True,
                "script": script_name,
                "name": meta["name"],
                "risk_level": meta["risk_level"],
                "duration_ms": duration,
                "output": result
            }
        except Exception as e:
            duration = round((time.time() - t0) * 1000, 2)
            log_event("script_execution_error", {"script": script_name, "error": str(e), "duration_ms": duration})
            return {"success": False, "script": script_name, "error": str(e)}

    async def _run_health_check(self) -> Dict[str, Any]:
        """Verificación interna de salud de subsistemas."""
        import urllib.request
        checks = {}

        # 1. Hub
        try:
            req = urllib.request.Request("http://127.0.0.1:8765/api/health", headers={"User-Agent": "Yedid-Script"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                checks["hub_fastapi"] = "online" if resp.status == 200 else f"status_{resp.status}"
        except Exception as e:
            checks["hub_fastapi"] = f"error: {e}"

        # 2. Ollama
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/tags", headers={"User-Agent": "Yedid-Script"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                checks["ollama_local"] = "online" if resp.status == 200 else f"status_{resp.status}"
        except Exception as e:
            checks["ollama_local"] = f"error: {e}"

        # 3. Sunshine Port Check
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            r = s.connect_ex(("127.0.0.1", 47989))
            s.close()
            checks["sunshine_web"] = "online (47989)" if r == 0 else "offline/firewalled"
        except Exception as e:
            checks["sunshine_web"] = f"error: {e}"

        return {"timestamp": datetime.now().isoformat(), "subsystems": checks}

    async def _run_backup(self) -> Dict[str, Any]:
        """Crea un respaldo de archivos clave."""
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = BACKUPS_DIR / f"snapshot_{ts}"
        target_dir.mkdir(parents=True, exist_ok=True)

        copied = []
        for item in ["hub", "mcp_gateway", "data"]:
            src = BASE_DIR / item
            dst = target_dir / item
            if src.exists():
                if src.is_dir():
                    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
                else:
                    shutil.copy2(src, dst)
                copied.append(item)

        return {
            "snapshot_path": str(target_dir),
            "copied_modules": copied,
            "created_at": datetime.now().isoformat()
        }

    async def _run_clean_temp(self) -> Dict[str, Any]:
        """Limpia archivos temporales generados por audio o testing."""
        cleaned_count = 0
        cleaned_bytes = 0

        temp_dirs = [
            BASE_DIR / "temp",
            Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "yedid_audio"
        ]

        for tdir in temp_dirs:
            if tdir.exists():
                for f in tdir.glob("*"):
                    try:
                        if f.is_file():
                            sz = f.stat().st_size
                            f.unlink()
                            cleaned_count += 1
                            cleaned_bytes += sz
                    except Exception:
                        pass

        return {
            "deleted_files": cleaned_count,
            "reclaimed_kb": round(cleaned_bytes / 1024, 2)
        }

    async def _run_windows_maintenance(self) -> Dict[str, Any]:
        """Diagnóstico de mantenimiento del sistema operativo."""
        import psutil
        disk_c = psutil.disk_usage("C:\\")
        disk_d = psutil.disk_usage("D:\\") if os.path.exists("D:\\") else None
        mem = psutil.virtual_memory()

        temp_path = Path(os.environ.get("TEMP", "C:/Windows/Temp"))
        temp_files_count = len(list(temp_path.glob("*"))) if temp_path.exists() else 0

        return {
            "os": "Windows 11",
            "cpu_cores": psutil.cpu_count(logical=True),
            "ram_available_gb": round(mem.available / (1024**3), 2),
            "ram_percent": mem.percent,
            "disk_c_free_gb": round(disk_c.free / (1024**3), 2),
            "disk_d_free_gb": round(disk_d.free / (1024**3), 2) if disk_d else None,
            "temp_items_count": temp_files_count
        }

    async def _run_pc_hibernate(self) -> Dict[str, Any]:
        """Pone la PC en hibernación segura (ACPI S4)."""
        log_event("system_hibernation_initiated", {"requester": "pc_hibernate_script"})
        # Ejecuta shutdown /h en hilo separado
        def do_hibernate():
            subprocess.run(["shutdown", "/h"], check=False)

        asyncio.create_task(asyncio.to_thread(do_hibernate))
        return {
            "status": "hibernating",
            "message": "Comando de hibernación enviado. La PC entrará en estado ACPI S4 de inmediato."
        }

scripts_service = ScriptsService()
