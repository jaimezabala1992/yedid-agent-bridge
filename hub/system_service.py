"""Servicio de monitoreo y control seguro de la computadora de Jaime."""
import psutil
import socket
from typing import Any, Dict
from .audit import log_event

def get_system_vitals() -> Dict[str, Any]:
    """Obtiene métricas de CPU, RAM, almacenamiento y servicios clave."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk_c = psutil.disk_usage("C:\\")

        disk_d_info = None
        try:
            disk_d = psutil.disk_usage("D:\\")
            disk_d_info = {
                "total_gb": round(disk_d.total / (1024**3), 1),
                "free_gb": round(disk_d.free / (1024**3), 1),
                "percent_used": disk_d.percent
            }
        except Exception:
            pass

        # Verificar Sunshine para streaming de juegos
        sunshine_active = False
        for p in psutil.process_iter(['name']):
            try:
                if p.info['name'] and 'sunshine' in p.info['name'].lower():
                    sunshine_active = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        vitals = {
            "hostname": socket.gethostname(),
            "cpu_percent": cpu_pct,
            "ram": {
                "total_gb": round(mem.total / (1024**3), 1),
                "free_gb": round(mem.available / (1024**3), 1),
                "percent_used": mem.percent
            },
            "disk_c": {
                "total_gb": round(disk_c.total / (1024**3), 1),
                "free_gb": round(disk_c.free / (1024**3), 1),
                "percent_used": disk_c.percent
            },
            "disk_d": disk_d_info,
            "sunshine_gaming_server": "Activo" if sunshine_active else "Inactivo",
            "status": "Óptimo"
        }
        log_event("system_vitals_check", {"cpu": cpu_pct, "ram_used_pct": mem.percent})
        return vitals
    except Exception as e:
        err_res = {"error": str(e), "status": "Error leyendo métricas"}
        log_event("system_vitals_error", err_res)
        return err_res
