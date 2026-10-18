"""
hub/process_guard.py

Gestor de ciclo de vida de procesos de agentes mediante Windows Job Objects (Kernel NT):
- Vincula procesos de agy.exe / Python a un Job Object de Windows con JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
- Garantiza que al interrumpir, cancelar una tarea o ante una caída o reinicio del Hub,
  el kernel de Windows termine FORZOSAMENTE todo el árbol de procesos hijos, eliminando
  completamente la posibilidad de procesos huérfanos.
- Provee utilidades para auditar y limpiar procesos huérfanos de agy.exe o adaptadores en el arranque.
"""

import ctypes
from ctypes import wintypes
import logging
import os
import subprocess
from typing import Dict, Optional, Set

logger = logging.getLogger("yedid_process_guard")

# Constantes de Win32 API
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
PROCESS_ALL_ACCESS = 0x1F0FFF

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_uint64),
        ('WriteOperationCount', ctypes.c_uint64),
        ('OtherOperationCount', ctypes.c_uint64),
        ('ReadTransferCount', ctypes.c_uint64),
        ('WriteTransferCount', ctypes.c_uint64),
        ('OtherTransferCount', ctypes.c_uint64),
    ]

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', wintypes.LARGE_INTEGER),
        ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    ]

class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class WindowsJobGuard:
    """Administrador de Job Objects en Windows para contención y terminación garantizada de procesos."""

    def __init__(self):
        self._is_windows = (os.name == 'nt')
        self._kernel32 = ctypes.windll.kernel32 if self._is_windows else None
        # Mapa de task_id -> (job_handle, pid)
        self._active_jobs: Dict[str, int] = {}
        self._active_pids: Dict[str, int] = {}

    def attach_process(self, task_id: str, pid: int) -> bool:
        """
        Crea un Job Object de Windows con JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        y asocia el proceso especificado por su PID.
        """
        if not self._is_windows or not self._kernel32:
            self._active_pids[task_id] = pid
            return False

        try:
            hJob = self._kernel32.CreateJobObjectW(None, None)
            if not hJob:
                err = self._kernel32.GetLastError()
                logger.warning(f"[PROCESS GUARD] No se pudo crear JobObject para {task_id}: error {err}")
                self._active_pids[task_id] = pid
                return False

            # Configurar límite: KILL_ON_JOB_CLOSE
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            res = self._kernel32.SetInformationJobObject(
                hJob,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info)
            )
            if not res:
                err = self._kernel32.GetLastError()
                logger.warning(f"[PROCESS GUARD] SetInformationJobObject falló para {task_id}: error {err}")
                self._kernel32.CloseHandle(hJob)
                self._active_pids[task_id] = pid
                return False

            # Abrir el proceso con permisos de asignación
            hProc = self._kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
            if not hProc:
                err = self._kernel32.GetLastError()
                logger.warning(f"[PROCESS GUARD] OpenProcess falló para PID {pid}: error {err}")
                self._kernel32.CloseHandle(hJob)
                self._active_pids[task_id] = pid
                return False

            assign_res = self._kernel32.AssignProcessToJobObject(hJob, hProc)
            self._kernel32.CloseHandle(hProc)

            if not assign_res:
                err = self._kernel32.GetLastError()
                logger.warning(f"[PROCESS GUARD] AssignProcessToJobObject falló para PID {pid}: error {err}")
                self._kernel32.CloseHandle(hJob)
                self._active_pids[task_id] = pid
                return False

            self._active_jobs[task_id] = hJob
            self._active_pids[task_id] = pid
            logger.info(f"[PROCESS GUARD] Tarea {task_id} (PID {pid}) asegurada en JobObject de Windows kernel (KILL_ON_JOB_CLOSE).")
            return True

        except Exception as e:
            logger.error(f"[PROCESS GUARD] Error asignando proceso a JobObject: {e}")
            self._active_pids[task_id] = pid
            return False

    def terminate_task(self, task_id: str, exit_code: int = 1) -> bool:
        """
        Termina de inmediato y por el kernel de Windows todos los procesos asociados a la tarea.
        Cierra el Job Object, lo que garantiza que no queden procesos hijos huérfanos.
        """
        terminated = False
        if self._is_windows and self._kernel32:
            hJob = self._active_jobs.pop(task_id, None)
            if hJob:
                try:
                    self._kernel32.TerminateJobObject(hJob, exit_code)
                    self._kernel32.CloseHandle(hJob)
                    terminated = True
                    logger.info(f"[PROCESS GUARD] JobObject para tarea {task_id} terminado y cerrado exitosamente.")
                except Exception as e:
                    logger.warning(f"[PROCESS GUARD] Error al terminar JobObject para {task_id}: {e}")

        # Como respaldo secundario, verificar el PID directamente
        pid = self._active_pids.pop(task_id, None)
        if pid:
            try:
                os.kill(pid, 9)
                terminated = True
            except Exception:
                pass

        return terminated

    def detach_task(self, task_id: str) -> None:
        """Limpia los identificadores cuando una tarea finaliza con éxito de forma natural."""
        self._active_pids.pop(task_id, None)
        if self._is_windows and self._kernel32:
            hJob = self._active_jobs.pop(task_id, None)
            if hJob:
                try:
                    self._kernel32.CloseHandle(hJob)
                except Exception:
                    pass

    def cleanup_all(self) -> None:
        """Termina todos los trabajos activos en apagado del servidor."""
        tasks = list(self._active_jobs.keys())
        for tid in tasks:
            self.terminate_task(tid)

    @staticmethod
    def cleanup_orphan_agy_processes() -> int:
        """
        Escanea y finaliza únicamente procesos huérfanos de agy.exe lanzados en modo headless
        por el adaptador de tareas (--sandbox --disable-slash-commands) cuyo proceso padre ya no exista.
        Nunca termina sesiones interactivas de Antigravity CLI.
        """
        if os.name != 'nt':
            return 0

        terminated = 0
        try:
            import psutil
            current_pid = os.getpid()
            protected_pids: Set[int] = {current_pid}
            try:
                proc = psutil.Process(current_pid)
                for parent in proc.parents():
                    protected_pids.add(parent.pid)
            except Exception:
                pass

            for p in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
                try:
                    pname = (p.info.get('name') or '').lower()
                    if pname not in ('agy.exe', 'agy'):
                        continue
                    pid = p.info['pid']
                    if pid in protected_pids:
                        continue
                    cmdline = ' '.join(p.info.get('cmdline') or [])
                    if '--sandbox' in cmdline and '--disable-slash-commands' in cmdline:
                        ppid = p.info.get('ppid')
                        if not ppid or not psutil.pid_exists(ppid):
                            p.kill()
                            terminated += 1
                            logger.info(f"[PROCESS GUARD] Proceso agy.exe huérfano terminado: PID {pid}")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            logger.warning(f"[PROCESS GUARD] Error comprobando procesos huérfanos de agy: {e}")

        return terminated


process_guard = WindowsJobGuard()
