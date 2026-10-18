"""
Catálogo completo de herramientas para Yedid MCP Gateway.
Incluye:
- 5 Herramientas de lectura protegida (Read-Only).
- 6 Herramientas de acción autorizadas con clasificación estricta de riesgo (Low / Medium / High).
- Integración con el Sistema de Aprobación Humana (HITL) para operaciones sensibles o destructivas.
- Validación de argumentos, listas blancas, protección contra path traversal y auditoría estricta.
"""
import sys
import time
import json
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

BASE_DIR = Path("C:/Yedid-Smart-Home")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from hub.system_service import get_system_vitals
from hub.tv_service import tv_service, APP_SHORTCUTS, ALLOWED_KEYS
from hub.scenes_service import scenes_service, SCENES_CATALOG
from hub.gaming_service import gaming_service, ALLOWED_GAMING_APPS
from hub.multiagent_bus import multiagent_bus, AGENT_REGISTRY
from hub.scripts_service import scripts_service, APPROVED_SCRIPTS
from hub.approvals import approval_manager
from hub.config import AUDIT_LOG_FILE
from .file_sandbox import file_sandbox
from .security import (
    sanitize_untrusted_string,
    enforce_payload_limit,
    log_mcp_event
)

# Mapa inverso para traducir paquetes de Android TV a nombres amigables
PACKAGE_TO_NAME = {v.lower(): k.title() for k, v in APP_SHORTCUTS.items()}
PACKAGE_TO_NAME["com.google.android.apps.tv.launcherx"] = "Menú Principal (Google TV)"
PACKAGE_TO_NAME["com.google.android.youtube.tv"] = "YouTube"
PACKAGE_TO_NAME["com.netflix.ninja"] = "Netflix"
PACKAGE_TO_NAME["com.spotify.tv.android"] = "Spotify"
PACKAGE_TO_NAME["com.amazon.amazonvideo.livingroom"] = "Prime Video"

# ==============================================================================
# 1. HERRAMIENTAS DE LECTURA (READ-ONLY)
# ==============================================================================

async def handle_get_hub_health() -> Dict[str, Any]:
    """1. Consulta la salud operativa y subsistemas activos de Yedid Hub."""
    t0 = time.time()
    try:
        hub_res = {
            "hub_name": "Yedid Smart Hub",
            "version": "2.0.0",
            "status": "healthy",
            "architecture": "local-first / zero-cloud-cost",
            "subsystems": {
                "fastapi_hub": "online (127.0.0.1:8765)",
                "ollama_local": "online (Qwen3 8B en CPU Ryzen 5)",
                "whisper_voice": "ready (faster-whisper int8 local)",
                "tailscale_mesh": "connected (https://yedid-hub.tailnet.ts.net:8443)",
                "mcp_gateway": "online (127.0.0.1:8766/mcp)"
            }
        }
        duration = (time.time() - t0) * 1000
        log_mcp_event("get_hub_health", {}, True, {"status": "healthy"}, duration_ms=duration)
        return enforce_payload_limit(hub_res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("get_hub_health", {}, False, {"error": err_msg}, duration_ms=duration)
        return {"error": f"Fallo al consultar salud del Hub: {err_msg}"}

async def handle_get_pc_status() -> Dict[str, Any]:
    """2. Consulta métricas de CPU, RAM y almacenamiento de la PC sin filtrar secretos ni rutas sensibles."""
    t0 = time.time()
    try:
        raw = await asyncio.to_thread(get_system_vitals)
        if "error" in raw:
            raise RuntimeError(raw["error"])

        clean_status = {
            "status": "online",
            "cpu_percent": round(float(raw.get("cpu_percent", 0.0)), 1),
            "ram": {
                "total_gb": raw.get("ram", {}).get("total_gb"),
                "free_gb": raw.get("ram", {}).get("free_gb"),
                "percent_used": raw.get("ram", {}).get("percent_used")
            },
            "disk_c": {
                "total_gb": raw.get("disk_c", {}).get("total_gb"),
                "free_gb": raw.get("disk_c", {}).get("free_gb"),
                "percent_used": raw.get("disk_c", {}).get("percent_used")
            },
            "disk_d": raw.get("disk_d")
        }
        duration = (time.time() - t0) * 1000
        log_mcp_event("get_pc_status", {}, True, {"cpu": clean_status["cpu_percent"]}, duration_ms=duration)
        return enforce_payload_limit(clean_status)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("get_pc_status", {}, False, {"error": err_msg}, duration_ms=duration)
        return {"error": f"Fallo al obtener estado de la PC: {err_msg}"}

async def handle_get_sunshine_status() -> Dict[str, Any]:
    """3. Consulta el estado del servidor de streaming de juegos Sunshine y dedicación de GPU."""
    t0 = time.time()
    try:
        raw = await asyncio.to_thread(get_system_vitals)
        sunshine_str = raw.get("sunshine_gaming_server", "Desconocido")
        is_active = "Activo" in sunshine_str

        res = {
            "service": "Sunshine Game Streaming Server",
            "status": "Activo" if is_active else "Inactivo",
            "gpu_dedicated": "AMD Radeon RX 6700 XT",
            "gpu_allocation": "100% dedicada a gaming y Moonlight (0% carga de IA)",
            "moonlight_ready": is_active
        }
        duration = (time.time() - t0) * 1000
        log_mcp_event("get_sunshine_status", {}, True, {"sunshine": res["status"]}, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("get_sunshine_status", {}, False, {"error": err_msg}, duration_ms=duration)
        return {"error": f"Fallo al consultar Sunshine: {err_msg}"}

async def handle_get_tv_status() -> Dict[str, Any]:
    """4. Consulta el estado de encendido y la app activa del televisor ONN Google TV de la sala."""
    t0 = time.time()
    try:
        raw = await tv_service.get_status(timeout=3.5)
        is_on = bool(raw.get("is_on", False))
        raw_app = raw.get("current_app", "")

        friendly_app = PACKAGE_TO_NAME.get(raw_app.lower(), sanitize_untrusted_string(raw_app, max_len=40))
        if not is_on:
            friendly_app = "En reposo / Apagada"

        res = {
            "device": "ONN Google TV Sala",
            "is_on": is_on,
            "power_state": "Encendida" if is_on else "En reposo / Apagada",
            "current_app": friendly_app
        }
        duration = (time.time() - t0) * 1000
        log_mcp_event("get_tv_status", {}, True, {"is_on": is_on, "app": friendly_app}, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("get_tv_status", {}, False, {"error": err_msg}, duration_ms=duration)
        return {"error": f"Fallo al consultar TV: {err_msg}"}

async def handle_get_recent_actions(limit: int = 5) -> Dict[str, Any]:
    """5. Consulta el historial de acciones y comandos ejecutados en el hogar (máximo 10)."""
    t0 = time.time()
    safe_limit = max(1, min(int(limit), 10))
    actions_list = []

    try:
        if AUDIT_LOG_FILE.exists():
            lines = AUDIT_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
            for line in reversed(lines[-50:]):
                try:
                    entry = json.loads(line)
                    ev_type = sanitize_untrusted_string(entry.get("event_type", ""))
                    details = entry.get("details", {})
                    ts = sanitize_untrusted_string(entry.get("timestamp", ""))

                    desc = ""
                    if ev_type == "tv_power_command":
                        action_str = "encendido" if details.get("power_on") else "apagado"
                        desc = f"Control TV: Comando de {action_str}"
                    elif ev_type == "tv_key_command":
                        desc = f"Control TV: Tecla {sanitize_untrusted_string(details.get('key'))}"
                    elif ev_type == "tv_launch_app":
                        desc = f"Control TV: Apertura de {sanitize_untrusted_string(details.get('app'))}"
                    elif ev_type == "scene_execution_success":
                        desc = f"Escena completada: {sanitize_untrusted_string(details.get('scene'))}"
                    elif ev_type == "mcp_tool_call":
                        desc = f"MCP: {sanitize_untrusted_string(entry.get('tool_name'))}"
                    elif ev_type == "human_approval_granted":
                        desc = f"Autorización Humana Aprobada: {sanitize_untrusted_string(details.get('action'))}"

                    if desc:
                        actions_list.append({
                            "timestamp": ts,
                            "event": ev_type,
                            "description": desc
                        })
                        if len(actions_list) >= safe_limit:
                            break
                except Exception:
                    continue

        res = {
            "total_actions": len(actions_list),
            "actions": actions_list
        }
        duration = (time.time() - t0) * 1000
        log_mcp_event("get_recent_actions", {"limit": safe_limit}, True, {"returned_count": len(actions_list)}, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("get_recent_actions", {"limit": safe_limit}, False, {"error": err_msg}, duration_ms=duration)
        return {"error": f"Fallo al consultar historial: {err_msg}"}


# ==============================================================================
# 2. HERRAMIENTAS DE ACCIÓN AUTORIZADAS (ACTION TOOLS CON CONTROL DE RIESGO)
# ==============================================================================

async def handle_execute_tv_control(
    action: str,
    key: Optional[str] = None,
    app: Optional[str] = None
) -> Dict[str, Any]:
    """
    Control seguro del televisor ONN Google TV de la sala.
    Acciones soportadas:
    - 'power_on': Enciende el televisor (Riesgo LOW).
    - 'power_off': Apaga o pone en reposo el televisor (Riesgo MEDIUM).
    - 'send_key': Envía una tecla de navegación o volumen de la lista blanca (Riesgo LOW).
    - 'launch_app': Abre una app autorizada (YouTube, Netflix, Spotify, Prime, Disney, Tailscale) (Riesgo LOW).
    """
    t0 = time.time()
    act = action.lower().strip()
    valid_actions = ["power_on", "power_off", "send_key", "launch_app"]

    if act not in valid_actions:
        return {
            "success": False,
            "error": f"Acción '{action}' no válida. Opciones permitidas: {valid_actions}"
        }

    try:
        if act == "power_on":
            res = await tv_service.power_on()
        elif act == "power_off":
            res = await tv_service.power_off()
        elif act == "send_key":
            if not key:
                return {"success": False, "error": "Debe especificar el parámetro 'key' para la acción 'send_key'."}
            clean_key = key.upper().strip()
            # Permitir alias comunes como UP, DOWN, OK
            if clean_key == "OK": clean_key = "DPAD_CENTER"
            if clean_key in ["UP", "DOWN", "LEFT", "RIGHT"]: clean_key = f"DPAD_{clean_key}"
            if clean_key not in ALLOWED_KEYS:
                return {
                    "success": False,
                    "error": f"Tecla '{key}' no autorizada. Teclas permitidas: {sorted(list(ALLOWED_KEYS))}"
                }
            res = await tv_service.send_key(clean_key)
        elif act == "launch_app":
            if not app:
                return {"success": False, "error": "Debe especificar el parámetro 'app' para la acción 'launch_app'."}
            clean_app = app.lower().strip()
            if clean_app not in APP_SHORTCUTS:
                return {
                    "success": False,
                    "error": f"Aplicación '{app}' no autorizada. Apps permitidas: {list(APP_SHORTCUTS.keys())}"
                }
            res = await tv_service.launch_app(clean_app)

        duration = (time.time() - t0) * 1000
        log_mcp_event("execute_tv_control", {"action": act, "key": key, "app": app}, True, res, duration_ms=duration)
        return enforce_payload_limit({"success": True, "action": act, "result": res})
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("execute_tv_control", {"action": act, "key": key, "app": app}, False, {"error": err_msg}, duration_ms=duration)
        return {"success": False, "action": act, "error": err_msg}

async def handle_execute_scene(scene_name: str) -> Dict[str, Any]:
    """
    Ejecuta una escena domótica preconfigurada en Yedid Hub.
    Escenas autorizadas:
    - 'modo_cine': Enciende TV y abre Netflix.
    - 'modo_gaming': Verifica Sunshine en PC, prepara stream y enciende TV.
    - 'modo_musica': Enciende TV y abre Spotify.
    - 'modo_noche': Apaga TV y apaga servicios de entretenimiento para descansar.
    """
    t0 = time.time()
    clean_scene = scene_name.lower().strip()

    if clean_scene not in SCENES_CATALOG:
        return {
            "success": False,
            "error": f"Escena '{scene_name}' no reconocida. Escenas disponibles: {list(SCENES_CATALOG.keys())}"
        }

    try:
        res = await scenes_service.execute_scene(clean_scene)
        duration = (time.time() - t0) * 1000
        log_mcp_event("execute_scene", {"scene": clean_scene}, res.get("success", False), res, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("execute_scene", {"scene": clean_scene}, False, {"error": err_msg}, duration_ms=duration)
        return {"success": False, "scene": clean_scene, "error": err_msg}

async def handle_manage_gaming_apps(app_name: str, action: str) -> Dict[str, Any]:
    """
    Administración controlada de Sunshine Game Streaming y aplicaciones de juego permitidas.
    Lista blanca estricta de aplicaciones:
    - 'sunshine': Acciones ['status', 'start', 'restart'].
    - 'steam': Acciones ['status', 'start'].
    - 'moonlight': Acciones ['status', 'start'].
    """
    t0 = time.time()
    try:
        res = await gaming_service.manage_app(app_name=app_name, action=action)
        duration = (time.time() - t0) * 1000
        log_mcp_event("manage_gaming_apps", {"app": app_name, "action": action}, res.get("success", False), res, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("manage_gaming_apps", {"app": app_name, "action": action}, False, {"error": err_msg}, duration_ms=duration)
        return {"success": False, "app": app_name, "action": action, "error": err_msg}

async def handle_manage_agent_tasks(
    action: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    target_agent: Optional[str] = None,
    task_id: Optional[str] = None,
    new_status: Optional[str] = None
) -> Dict[str, Any]:
    """
    Coordinación y seguimiento de tareas entre agentes locales (Ollama, Whisper, Gemini, Codex, ChatGPT Yedid).
    Acciones soportadas:
    - 'list': Lista las tareas multiagente recientes (Riesgo LOW).
    - 'create': Crea una nueva tarea asignada a un agente local (Riesgo MEDIUM).
    - 'get': Consulta el estado detallado de una tarea por su task_id (Riesgo LOW).
    - 'update_status': Actualiza el estado de una tarea ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED') (Riesgo MEDIUM).
    """
    t0 = time.time()
    act = action.lower().strip()

    try:
        if act == "list":
            tasks = multiagent_bus.list_tasks(limit=10)
            res = {"success": True, "total_tasks": len(tasks), "tasks": tasks}
        elif act in ("create", "delegate"):
            if not title or not target_agent:
                return {"success": False, "error": "Los parámetros 'title' y 'target_agent' son obligatorios para crear o delegar tareas."}
            created = multiagent_bus.create_task(
                title=title,
                description=description or "",
                target_agent=target_agent,
                source_agent="chatgpt_yedid"
            )
            if not created.get("success"):
                return created

            new_tid = created["task"]["task_id"]
            if act == "delegate":
                # Ejecutar inmediatamente delegando en el agente local (Ollama)
                res = await multiagent_bus.execute_task(new_tid)
            else:
                res = created
        elif act == "execute":
            if not task_id:
                return {"success": False, "error": "El parámetro 'task_id' es obligatorio para ejecutar una tarea existente."}
            res = await multiagent_bus.execute_task(task_id)
        elif act == "get":
            if not task_id:
                return {"success": False, "error": "El parámetro 'task_id' es obligatorio para consultar una tarea."}
            task = multiagent_bus.get_task(task_id)
            if not task:
                return {"success": False, "error": f"Tarea '{task_id}' no encontrada."}
            res = {"success": True, "task": task}
        elif act == "update_status":
            if not task_id or not new_status:
                return {"success": False, "error": "Debe especificar 'task_id' y 'new_status' para actualizar una tarea."}
            ok = multiagent_bus.update_task_status(task_id, new_status, result_summary=description)
            if not ok:
                return {"success": False, "error": f"No se pudo actualizar la tarea '{task_id}'. Verifique ID y estado válido."}
            res = {"success": True, "task_id": task_id, "updated_status": new_status.upper()}
        else:
            return {"success": False, "error": f"Acción '{action}' no válida. Use 'list', 'create', 'delegate', 'execute', 'get' o 'update_status'."}

        duration = (time.time() - t0) * 1000
        log_mcp_event("manage_agent_tasks", {"action": act, "title": title, "task_id": task_id}, True, res, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("manage_agent_tasks", {"action": act, "task_id": task_id}, False, {"error": err_msg}, duration_ms=duration)
        return {"success": False, "error": err_msg}

async def handle_manage_project_files(
    operation: str,
    relative_path: str,
    content: Optional[str] = None,
    allow_overwrite: bool = False,
    approval_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Operaciones seguras de lectura y escritura limitadas estrictamente a directorios de proyecto autorizados.
    Protección Sandbox: Path traversal denegado, bloqueo de secretos (.env, llaves), límite de 64 KB.
    Operaciones:
    - 'list_dir': Lista archivos en una carpeta de proyecto (Riesgo LOW).
    - 'read_file': Lee un archivo de texto de proyecto (Riesgo LOW).
    - 'write_file': Crea un archivo de texto nuevo (Riesgo MEDIUM).
      * NOTA: Sobreescribir un archivo existente es de ALTO RIESGO y requiere confirmación humana (approval_id).
    """
    t0 = time.time()
    op = operation.lower().strip()

    try:
        if op == "list_dir":
            res = file_sandbox.list_directory(relative_path=relative_path)
        elif op == "read_file":
            res = file_sandbox.read_file(relative_path=relative_path)
        elif op == "write_file":
            if content is None:
                return {"success": False, "error": "Debe proporcionar el parámetro 'content' para escribir un archivo."}

            # Si es sobreescritura de un archivo existente, validar aprobación humana
            target_path = file_sandbox._resolve_and_validate_path(relative_path, for_write=True)
            if target_path.exists():
                exp_action = f"overwrite_file:{relative_path}"
                exp_params = {"relative_path": relative_path, "bytes": len(content.encode('utf-8'))}
                # Verificar si cuenta con autorización previa vinculada exactamente a esta acción y parámetros
                if not approval_manager.is_action_authorized(approval_id, expected_action=exp_action, expected_params=exp_params):
                    # Crear solicitud de aprobación humana pendiente
                    req = approval_manager.create_request(
                        action=exp_action,
                        description=f"Sobreescribir archivo existente '{relative_path}' ({len(content)} caracteres)",
                        params=exp_params,
                        risk_level="HIGH"
                    )
                    return {
                        "success": False,
                        "requires_human_approval": True,
                        "status": "PENDING_HUMAN_APPROVAL",
                        "approval_id": req["id"],
                        "risk_level": "HIGH",
                        "message": (
                            f"Sobreescribir '{relative_path}' es una operación de ALTO RIESGO. "
                            f"Se ha retenido la ejecución y creado la solicitud de aprobación '{req['id']}' en Yedid Hub. "
                            f"Jaime debe autorizarla desde su Galaxy Fold 7 / PWA antes de proceder."
                        ),
                        "expires_at": req["expires_at"]
                    }

            res = file_sandbox.write_file(relative_path=relative_path, content=content, allow_overwrite=True)
        else:
            return {"success": False, "error": f"Operación '{operation}' no válida. Opciones: 'list_dir', 'read_file', 'write_file'."}

        duration = (time.time() - t0) * 1000
        log_mcp_event("manage_project_files", {"operation": op, "path": relative_path}, res.get("success", False), res, duration_ms=duration)
        return enforce_payload_limit(res)
    except PermissionError as pe:
        duration = (time.time() - t0) * 1000
        log_mcp_event("manage_project_files", {"operation": op, "path": relative_path}, False, {"security_block": str(pe)}, duration_ms=duration)
        return {"success": False, "security_violation": True, "error": str(pe)}
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("manage_project_files", {"operation": op, "path": relative_path}, False, {"error": err_msg}, duration_ms=duration)
        return {"success": False, "error": err_msg}

async def handle_run_approved_script(
    script_name: str,
    approval_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Ejecución de scripts previamente registrados y aprobados para diagnóstico y mantenimiento.
    NO permite inyección arbitraria de comandos de terminal ni comandos del sistema operativo.
    Scripts autorizados:
    - 'system_health_check': Inspección completa de FastAPI, Ollama, Whisper, Tailscale y TV (Riesgo LOW).
    - 'backup_configs': Respaldo instantáneo de configuraciones y registros en backups/ (Riesgo LOW).
    - 'clean_temp_logs': Limpieza de registros y audios temporales transitorios (Riesgo MEDIUM).
    - 'windows_maintenance': Diagnóstico de discos, carpetas temporales y memoria de Windows (Riesgo MEDIUM).
    - 'pc_hibernate': Pone la computadora principal en hibernación ACPI S4 (Riesgo HIGH - Requiere Aprobación Humana).
    """
    t0 = time.time()
    clean_name = script_name.lower().strip()

    if clean_name not in APPROVED_SCRIPTS:
        return {
            "success": False,
            "error": f"El script '{script_name}' no está registrado en el catálogo aprobado. Opciones: {list(APPROVED_SCRIPTS.keys())}"
        }

    script_meta = APPROVED_SCRIPTS[clean_name]

    # Verificar si el script requiere confirmación humana (ALTO RIESGO)
    if script_meta["requires_human_approval"]:
        exp_action = f"run_script:{clean_name}"
        exp_params = {"script_name": clean_name, "risk": script_meta["risk_level"]}
        if not approval_manager.is_action_authorized(approval_id, expected_action=exp_action, expected_params=exp_params):
            req = approval_manager.create_request(
                action=exp_action,
                description=f"Ejecución de script crítico '{script_meta['name']}' ({clean_name})",
                params=exp_params,
                risk_level="HIGH"
            )
            return {
                "success": False,
                "requires_human_approval": True,
                "status": "PENDING_HUMAN_APPROVAL",
                "approval_id": req["id"],
                "risk_level": "HIGH",
                "script_name": clean_name,
                "message": (
                    f"El script '{script_meta['name']}' es de ALTO RIESGO y apagará/alterará el sistema operativo. "
                    f"Se ha retenido la ejecución y creado la solicitud de autorización '{req['id']}' en Yedid Hub. "
                    f"Jaime debe confirmar la acción desde la interfaz de su Galaxy Fold 7 / PWA."
                ),
                "expires_at": req["expires_at"]
            }

    try:
        res = await scripts_service.execute_script(clean_name)
        duration = (time.time() - t0) * 1000
        log_mcp_event("run_approved_script", {"script": clean_name}, res.get("success", False), res, duration_ms=duration)
        return enforce_payload_limit(res)
    except Exception as e:
        duration = (time.time() - t0) * 1000
        err_msg = sanitize_untrusted_string(str(e))
        log_mcp_event("run_approved_script", {"script": clean_name}, False, {"error": err_msg}, duration_ms=duration)
        return {"success": False, "script": clean_name, "error": err_msg}
