# -*- coding: utf-8 -*-
"""
agent_bridge/bridge_daemon.py

Demonio de ejecución en segundo plano para Yedid Agent Bridge.
- Previene procesos duplicados mediante archivo de bloqueo (lockfile).
- Consulta periódica con intervalo configurable (30-60s).
- Recuperación automática ante micro-cortes de red.
- Consumo ultra bajo de CPU/RAM (en reposo duerme en event loop).
- Apagado limpio y respetuoso.
"""

import os
import sys
import time
import signal
import asyncio
import logging
from pathlib import Path
from typing import Optional

from .config import load_bridge_config, DATA_DIR, ensure_data_dir, LOCK_FILE
from .dispatcher import BridgeDispatcher

logger = logging.getLogger("YedidAgentBridge.Daemon")


class BridgeDaemon:
    """Demonio de servicio para polling de tareas de GitHub."""

    def __init__(self):
        ensure_data_dir()
        self.config = load_bridge_config()
        self.dispatcher = BridgeDispatcher()
        self.running = False
        self._setup_logging()

    def _setup_logging(self):
        level_name = self.config.get("log_level", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, level_name, logging.INFO),
            format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

    def _acquire_lock(self) -> bool:
        """Adquiere el cerrojo para evitar procesos duplicados."""
        if LOCK_FILE.exists():
            try:
                old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
                # Comprobar si el proceso anterior sigue vivo en Windows
                import psutil
                if psutil.pid_exists(old_pid):
                    p = psutil.Process(old_pid)
                    if "python" in p.name().lower():
                        logger.warning(f"Ya existe una instancia de Yedid Agent Bridge en ejecución (PID {old_pid}). Abortando inicio duplicado.")
                        return False
            except Exception:
                pass
            # Si el PID ya no existe, borrar cerrojo huérfano
            LOCK_FILE.unlink(missing_ok=True)

        try:
            LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
            return True
        except Exception as e:
            logger.error(f"Error escribiendo lockfile: {e}")
            return False

    def _release_lock(self):
        """Libera el cerrojo de proceso."""
        try:
            if LOCK_FILE.exists():
                LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def run(self):
        """Bucle principal asíncrono del demonio."""
        if not self._acquire_lock():
            return 1

        self.running = True
        logger.info("Yedid Agent Bridge iniciado exitosamente.")

        # Manejo de señales de terminación
        def handle_signal(*args):
            logger.info("Señal de parada recibida. Cerrando Bridge limpiamente...")
            self.running = False

        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(s, handle_signal)

        interval = max(15, int(self.config.get("poll_interval_seconds", 30)))
        backoff_error = 0

        try:
            while self.running:
                try:
                    result = await self.dispatcher.poll_and_process_cycle()
                    status = result.get("status")

                    if status == "ok":
                        proc = result.get("processed_count", 0)
                        if proc > 0:
                            logger.info(f"Ciclo completado: {proc} tarea(s) procesada(s).")
                        backoff_error = 0
                    elif status == "paused":
                        pass
                    elif status == "no_token":
                        logger.debug("Bridge en espera de token de GitHub...")
                    elif status in ("connection_error", "fetch_error"):
                        backoff_error = min(60, backoff_error + 15)
                        logger.warning(f"Error transitorio de red: {result.get('error')}. Reintentando con backoff ({backoff_error}s)...")
                    elif status == "security_violation":
                        logger.critical(f"ALERTA DE SEGURIDAD: {result.get('error')}. El demonio pausará consultas hasta corrección.")
                        await asyncio.sleep(60)

                except Exception as e:
                    logger.error(f"Excepción en ciclo de polling: {e}")
                    backoff_error = min(60, backoff_error + 10)

                sleep_time = interval + backoff_error
                # Dormir de forma fraccionada para responder rápidamente a SIGINT
                for _ in range(int(sleep_time * 2)):
                    if not self.running:
                        break
                    await asyncio.sleep(0.5)

        finally:
            self._release_lock()
            logger.info("Yedid Agent Bridge detenido.")

        return 0

def main():
    daemon = BridgeDaemon()
    try:
        sys.exit(asyncio.run(daemon.run()))
    except KeyboardInterrupt:
        logger.info("Interrupción manual de teclado detectada.")
        daemon._release_lock()
        sys.exit(0)

if __name__ == "__main__":
    main()
