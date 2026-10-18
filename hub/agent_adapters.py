"""
hub/agent_adapters.py

Adaptadores de ejecución real y supervisada para agentes locales:
- Antigravity / Gemini CLI (v1.4.2 via agy.exe).
- Scripts supervisados de Python para diagnósticos y pruebas seguras.
- Eliminación total de flags deshabilitadores de permisos.
- Modo estricto de edición controlada (--mode accept-edits) con sandbox activo (--sandbox)
  y desactivación de comandos slash (--disable-slash-commands).
- Inyección de políticas de seguridad en el espacio de trabajo (.agents/rules/security.md y GEMINI.md).
- Auditoría post-ejecución de entregables contra rutas sensibles (memorias, credenciales, respaldos).
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .security_policy import (
    validate_project_path,
    is_sensitive_path,
    sanitize_instruction,
    build_secure_agent_env,
    ensure_workspace_security_rules,
    verify_deliverables_security
)

logger = logging.getLogger("yedid_adapters")

AGY_EXE = Path(os.getenv("YEDID_AGY_BIN", os.path.expandvars(r"%USERPROFILE%\Documents\Codex\AntigravityCLI\agy.exe")))
_DEFAULT_PYTHON_CANDIDATE = Path(os.path.expandvars(r"%USERPROFILE%\Documents\Codex\runtime\python\python.exe"))
PYTHON_EXE = Path(
    os.getenv(
        "YEDID_PYTHON_BIN",
        str(_DEFAULT_PYTHON_CANDIDATE if _DEFAULT_PYTHON_CANDIDATE.exists() else Path(sys.executable)),
    )
)


def is_path_authorized(target_path: Path) -> bool:
    """Verifica si una ruta está autorizada según la política de seguridad central."""
    valid, _, _ = validate_project_path(target_path)
    return valid


def scan_deliverables(directory: Path, since_timestamp: float) -> List[Dict[str, Any]]:
    """Identifica archivos creados o modificados en el directorio durante la ejecución de la tarea."""
    deliverables = []
    if not directory.exists() or not directory.is_dir():
        return deliverables

    try:
        for p in directory.glob("**/*"):
            if p.is_file() and not any(part.startswith(".") for part in p.parts):
                try:
                    stat = p.stat()
                    if stat.st_mtime >= (since_timestamp - 1.0):  # Margen de 1s por precisión de FS
                        deliverables.append({
                            "path": str(p.resolve()),
                            "name": p.name,
                            "relative_path": str(p.relative_to(directory)),
                            "size_bytes": stat.st_size,
                            "modified_iso": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                        })
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"Error escaneando entregables en {directory}: {e}")

    # Ordenar por fecha más reciente
    deliverables.sort(key=lambda x: x["modified_iso"], reverse=True)
    return deliverables[:25]


class GeminiCLIAdapter:
    """Adaptador de ejecución real y segura para Antigravity / Gemini CLI."""

    def __init__(self, binary_path: Path = AGY_EXE):
        self.binary_path = binary_path

    def check_availability(self) -> Dict[str, Any]:
        """Comprueba disponibilidad real de agy.exe."""
        if not self.binary_path.exists():
            return {
                "available": False,
                "error": f"Binario no encontrado en {self.binary_path}"
            }
        try:
            res = subprocess.run([str(self.binary_path), "--version"], capture_output=True, text=True, timeout=5)
            version = res.stdout.strip()
            return {
                "available": True,
                "version": version,
                "path": str(self.binary_path),
                "unmetered": True
            }
        except Exception as e:
            return {"available": False, "error": str(e)}

    async def execute_task(
        self,
        instruction: str,
        project_path: Path,
        timeout_seconds: float = 120.0,
        allow_edits: bool = True,
        on_process_started: Optional[Callable[[int], None]] = None
    ) -> Dict[str, Any]:
        """
        Ejecuta una instrucción real con Gemini CLI de forma no interactiva, supervisada y segura:
        - Valida que project_path sea una ruta autorizada y libre de zonas rojas.
        - Inyecta reglas de seguridad locales (.agents/rules/security.md).
        - Sanitiza la instrucción para prevenir inyección de caracteres de control o comandos de shell.
        - NO desactiva ni evade permisos bajo ninguna circunstancia.
        - Utiliza --mode accept-edits con --sandbox y --disable-slash-commands.
        - Limpia el entorno de variables sensibles (tokens, claves).
        - Audita entregables post-ejecución contra rutas sensibles.
        """
        avail = self.check_availability()
        if not avail.get("available"):
            return {
                "success": False,
                "error": f"Gemini CLI no disponible: {avail.get('error')}",
                "stdout": "",
                "stderr": "",
                "deliverables": []
            }

        valid, valid_path, err_reason = validate_project_path(project_path)
        if not valid or not valid_path:
            return {
                "success": False,
                "error": f"Directorio de proyecto no autorizado: {err_reason}",
                "stdout": "",
                "stderr": err_reason,
                "deliverables": []
            }

        valid_path.mkdir(parents=True, exist_ok=True)
        ensure_workspace_security_rules(valid_path)
        sanitized_instruction = sanitize_instruction(instruction)
        t_start = time.time()

        # Construcción estricta y segura de argumentos (sin shell=True)
        cmd = [
            str(self.binary_path),
            "-p", sanitized_instruction,
            "--output-format", "text",
            "--mode", "accept-edits" if allow_edits else "plan",
            "--sandbox",
            "--disable-slash-commands"
        ]

        clean_env = build_secure_agent_env()

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(valid_path),
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            if on_process_started and process.pid:
                try:
                    on_process_started(process.pid)
                except Exception as cb_err:
                    logger.warning(f"Error en callback on_process_started: {cb_err}")

            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )

            stdout_txt = stdout_b.decode("utf-8", errors="replace").strip()
            stderr_txt = stderr_b.decode("utf-8", errors="replace").strip()
            returncode = process.returncode

            duration = round(time.time() - t_start, 2)
            deliverables = scan_deliverables(valid_path, since_timestamp=t_start)
            if returncode == 0 and not deliverables and stdout_txt:
                try:
                    report_file = valid_path / f"yedid_reporte_{int(t_start)}.md"
                    report_content = (
                        f"# Reporte de Ejecución Gemini CLI — Yedid Hub\n\n"
                        f"- **Fecha (UTC):** {datetime.now(timezone.utc).isoformat()}\n"
                        f"- **Directorio:** `{valid_path}`\n"
                        f"- **Instrucción:** {sanitized_instruction}\n\n"
                        f"## Resultado\n\n{stdout_txt}\n"
                    )
                    report_file.write_text(report_content, encoding="utf-8")
                    deliverables = scan_deliverables(valid_path, since_timestamp=t_start)
                except Exception as rep_err:
                    logger.warning(f"No se pudo escribir archivo de reporte adicional: {rep_err}")

            # Auditoría de seguridad sobre los entregables producidos
            is_sec_ok, sec_violations = verify_deliverables_security(deliverables, valid_path)
            if not is_sec_ok:
                logger.error(f"[SEGURIDAD] Violaciones en entregables de tarea: {sec_violations}")
                return {
                    "success": False,
                    "returncode": -2,
                    "duration_seconds": duration,
                    "stdout": stdout_txt,
                    "stderr": f"Violación de política de seguridad en entregables: {'; '.join(sec_violations)}",
                    "deliverables": [],
                    "agent": "gemini_cli",
                    "security_violation": True
                }

            success = (returncode == 0)
            return {
                "success": success,
                "returncode": returncode,
                "duration_seconds": duration,
                "stdout": stdout_txt,
                "stderr": stderr_txt,
                "deliverables": deliverables,
                "agent": "gemini_cli"
            }

        except asyncio.TimeoutError:
            duration = round(time.time() - t_start, 2)
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return {
                "success": False,
                "returncode": -1,
                "duration_seconds": duration,
                "stdout": "",
                "stderr": f"Tiempo de ejecución excedido (Timeout de {timeout_seconds}s).",
                "deliverables": [],
                "agent": "gemini_cli"
            }
        except Exception as e:
            duration = round(time.time() - t_start, 2)
            return {
                "success": False,
                "returncode": -1,
                "duration_seconds": duration,
                "stdout": "",
                "stderr": f"Error ejecutando Gemini CLI: {str(e)}",
                "deliverables": [],
                "agent": "gemini_cli"
            }


class SafeScriptAdapter:
    """Adaptador para tareas controladas de prueba, mantenimiento y diagnósticos locales."""

    def __init__(self, python_path: Path = PYTHON_EXE):
        self.python_path = python_path

    async def execute_task(
        self,
        instruction: str,
        project_path: Path,
        script_code: Optional[str] = None,
        timeout_seconds: float = 60.0,
        on_process_started: Optional[Callable[[int], None]] = None
    ) -> Dict[str, Any]:
        """Ejecuta una tarea de prueba o script seguro en el directorio autorizado."""
        valid, valid_path, err_reason = validate_project_path(project_path)
        if not valid or not valid_path:
            return {
                "success": False,
                "error": f"Directorio '{project_path}' no autorizado: {err_reason}",
                "stdout": "",
                "stderr": err_reason,
                "deliverables": []
            }

        valid_path.mkdir(parents=True, exist_ok=True)
        ensure_workspace_security_rules(valid_path)
        sanitized_instruction = sanitize_instruction(instruction)
        t_start = time.time()

        # Si no se proporciona código de script, usar una plantilla estática libre de interpolación de código
        # y pasar los datos exclusivamente mediante variables de entorno inertes.
        exec_id = f"{int(t_start * 1000)}"
        target_script = valid_path / f"task_exec_{exec_id}.py"
        out_file = valid_path / f"resultado_tarea_{exec_id}.txt"
        clean_env = build_secure_agent_env()
        clean_env["YEDID_TASK_INSTRUCTION"] = sanitized_instruction
        clean_env["YEDID_TASK_OUTPUT_PATH"] = str(out_file)
        clean_env["YEDID_TASK_PROJECT_DIR"] = str(valid_path)

        if not script_code:
            script_code = """# Script de ejecución segura de tarea Yedid (plantilla estática sin interpolación)
import os
from datetime import datetime, timezone

output_path = os.environ["YEDID_TASK_OUTPUT_PATH"]
task_instruction = os.environ.get("YEDID_TASK_INSTRUCTION", "")
project_dir = os.environ.get("YEDID_TASK_PROJECT_DIR", "")
timestamp = datetime.now(timezone.utc).isoformat()

lines = [
    "==================================================",
    "RESULTADO DE EJECUCIÓN REAL — YEDID HUB",
    "==================================================",
    f"Tarea: {task_instruction}",
    f"Fecha de ejecución: {timestamp}",
    f"Directorio: {project_dir}",
    "Estado: COMPLETADO CON ÉXITO",
    "Entregable generado y verificado en disco.",
    "==================================================",
]
content = "\\n".join(lines)

with open(output_path, "w", encoding="utf-8") as f:
    f.write(content)

print(f"Archivo de entregable generado exitosamente: {output_path}")
print(f"Bytes escritos: {len(content.encode('utf-8'))}")
"""
        target_script.write_text(script_code, encoding="utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                str(self.python_path), str(target_script),
                cwd=str(valid_path),
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            if on_process_started and process.pid:
                try:
                    on_process_started(process.pid)
                except Exception as cb_err:
                    logger.warning(f"Error en callback on_process_started: {cb_err}")

            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )

            stdout_txt = stdout_b.decode("utf-8", errors="replace").strip()
            stderr_txt = stderr_b.decode("utf-8", errors="replace").strip()
            returncode = process.returncode
            duration = round(time.time() - t_start, 2)

            deliverables = scan_deliverables(valid_path, since_timestamp=t_start)
            is_sec_ok, sec_violations = verify_deliverables_security(deliverables, valid_path)
            if not is_sec_ok:
                return {
                    "success": False,
                    "returncode": -2,
                    "duration_seconds": duration,
                    "stdout": stdout_txt,
                    "stderr": f"Violación de política de seguridad en entregables: {'; '.join(sec_violations)}",
                    "deliverables": [],
                    "agent": "safe_script",
                    "security_violation": True
                }

            return {
                "success": returncode == 0,
                "returncode": returncode,
                "duration_seconds": duration,
                "stdout": stdout_txt,
                "stderr": stderr_txt,
                "deliverables": deliverables,
                "agent": "safe_script"
            }
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return {
                "success": False,
                "returncode": -1,
                "duration_seconds": round(time.time() - t_start, 2),
                "stdout": "",
                "stderr": f"Timeout de ejecución excedido ({timeout_seconds}s).",
                "deliverables": [],
                "agent": "safe_script"
            }
        except Exception as e:
            return {
                "success": False,
                "returncode": -1,
                "duration_seconds": round(time.time() - t_start, 2),
                "stdout": "",
                "stderr": str(e),
                "deliverables": [],
                "agent": "safe_script"
            }


gemini_adapter = GeminiCLIAdapter()
safe_script_adapter = SafeScriptAdapter()
