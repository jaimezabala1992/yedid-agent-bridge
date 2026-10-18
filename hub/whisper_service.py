"""Servicio de transcripción de voz local gestionado por proceso worker independiente.

Carga faster-whisper (tiny int8 en CPU) bajo demanda en un proceso separado,
permitiendo descargar el modelo tras periodos de inactividad (60s) y devolver el 100%
de la memoria RAM al sistema operativo Windows sin reiniciar FastAPI.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
import psutil

from .config import BASE_DIR
from .audit import log_event

logger = logging.getLogger("yedid_whisper")

class WhisperManager:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._last_used: float = 0.0

    def _ensure_worker_running(self):
        """Inicia el proceso worker si no se encuentra en ejecución."""
        if self._proc is not None and self._proc.poll() is None:
            return

        logger.info("Iniciando proceso worker independiente para faster-whisper...")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "hub.whisper_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(BASE_DIR)
            )

            # Esperar señal de inicialización lista ("ready")
            ready_line = self._proc.stdout.readline()
            if ready_line:
                ready_data = json.loads(ready_line.strip())
                if ready_data.get("status") == "ready":
                    logger.info(f"Worker faster-whisper operativo en PID: {self._proc.pid}")
                    self._last_used = time.time()
                    return

            raise RuntimeError(f"El worker de Whisper no devolvió confirmación 'ready': {ready_line}")

        except Exception as e:
            logger.error(f"Error iniciando proceso worker de Whisper: {e}")
            self.unload_worker()
            raise e

    def is_worker_alive(self) -> bool:
        """Verifica si el worker está actualmente vivo y ejecutándose."""
        return self._proc is not None and self._proc.poll() is None

    def get_worker_pid(self) -> Optional[int]:
        """Devuelve el PID del worker si está activo."""
        return self._proc.pid if self.is_worker_alive() else None

    def get_worker_memory_mb(self) -> float:
        """Calcula el consumo real de RAM (RSS) del proceso worker en MB."""
        if not self.is_worker_alive():
            return 0.0
        try:
            p = psutil.Process(self._proc.pid)
            return round(p.memory_info().rss / (1024 * 1024), 2)
        except Exception:
            return 0.0

    def get_idle_seconds(self) -> float:
        """Tiempo transcurrido desde la última transcripción realizada."""
        if not self.is_worker_alive() or self._last_used == 0:
            return 0.0
        return round(time.time() - self._last_used, 1)

    def unload_worker(self) -> bool:
        """Descarga el proceso worker y libera el 100% de la memoria a Windows."""
        with self._lock:
            if self._proc is None:
                return False

            pid = self._proc.pid
            logger.info(f"Descargando worker Whisper (PID {pid})...")

            try:
                if self._proc.poll() is None:
                    # Enviar comando de terminación limpia
                    try:
                        self._proc.stdin.write(json.dumps({"action": "exit"}) + "\n")
                        self._proc.stdin.flush()
                        self._proc.wait(timeout=2.0)
                    except Exception:
                        self._proc.terminate()
                        self._proc.wait(timeout=2.0)
            except Exception as e:
                logger.warning(f"Error cerrando worker Whisper suavemente: {e}. Forzando kill.")
                try:
                    self._proc.kill()
                except Exception:
                    pass
            finally:
                for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
                    if pipe and not pipe.closed:
                        try:
                            pipe.close()
                        except Exception:
                            pass
                self._proc = None
                self._last_used = 0.0
                log_event("whisper_worker_unloaded", {"pid": pid, "status": "memory_freed"})
                logger.info(f"Worker Whisper (PID {pid}) terminado. Memoria completamente liberada.")
                return True

    def transcribe_file(self, file_path: str, language: str = "es") -> str:
        """Delega la transcripción al worker aislado."""
        with self._lock:
            self._ensure_worker_running()

            req_payload = {
                "action": "transcribe",
                "file_path": file_path,
                "language": language
            }

            try:
                self._proc.stdin.write(json.dumps(req_payload) + "\n")
                self._proc.stdin.flush()

                resp_line = self._proc.stdout.readline()
                if not resp_line:
                    raise RuntimeError("El worker de Whisper cerró inesperadamente la comunicación.")

                resp_data = json.loads(resp_line.strip())
                if not resp_data.get("success"):
                    err = resp_data.get("error", "Error desconocido en transcripción")
                    raise RuntimeError(err)

                self._last_used = time.time()
                text = resp_data.get("text", "").strip()

                log_event("voice_transcription_success", {
                    "duration": resp_data.get("duration"),
                    "language": resp_data.get("language"),
                    "char_count": len(text),
                    "load_ms": resp_data.get("load_ms"),
                    "transcribe_ms": resp_data.get("transcribe_ms"),
                    "worker_pid": resp_data.get("pid")
                })
                return text

            except Exception as e:
                logger.error(f"Fallo en transcripción mediante worker: {e}")
                self.unload_worker()
                raise e

whisper_manager = WhisperManager()

def transcribe_audio_file(file_path: str, language: str = "es") -> str:
    """Función de compatibilidad para transcribir un archivo existente."""
    return whisper_manager.transcribe_file(file_path, language=language)

def transcribe_audio_bytes(audio_bytes: bytes, suffix: str = ".webm", language: str = "es") -> str:
    """Guarda bytes temporalmente, transcribe en worker aislado y limpia el archivo."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        return transcribe_audio_file(tmp_path, language=language)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def get_whisper_model():
    """Función de compatibilidad para código legado que consulta el modelo directamente."""
    from faster_whisper import WhisperModel
    return WhisperModel("tiny", device="cpu", compute_type="int8")
