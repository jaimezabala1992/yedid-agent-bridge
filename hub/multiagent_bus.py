"""Bus de coordinación multiagente local para Yedid Smart Home."""
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from .audit import log_event

AGENT_REGISTRY = {
    "chatgpt_yedid": {
        "name": "ChatGPT (Yedid Principal)",
        "type": "orchestrator_agent",
        "capabilities": ["high_level_planning", "home_management", "task_delegation"],
        "allowed_tools": [
            "get_hub_health", "get_pc_status", "get_sunshine_status", "get_tv_status", "get_recent_actions",
            "execute_tv_control", "execute_scene", "manage_gaming_apps", "manage_agent_tasks",
            "manage_project_files", "run_approved_script"
        ],
        "forbidden_tools": ["arbitrary_cmd_injection", "unrestricted_file_deletion"]
    },
    "ollama_qwen3": {
        "name": "Ollama Qwen3 8B",
        "type": "local_llm",
        "hardware": "CPU (Ryzen 5 5600X)",
        "capabilities": [
            "intent_classification", "tool_calling", "spanish_conversation",
            "agent_orchestration", "quota_audit", "task_handover"
        ],
        "allowed_tools": [
            "get_tv_status", "turn_on_tv", "turn_off_tv", "launch_tv_app", "send_tv_key",
            "get_pc_vitals", "run_scene", "propose_memory_to_save", "get_agents_status",
            "get_agent_tasks", "check_codex_quota", "get_project_context", "request_task_handover"
        ],
        "forbidden_tools": ["shell_exec", "file_system_write", "network_modify"]
    },
    "whisper_voice": {
        "name": "Faster-Whisper (Tiny)",
        "type": "audio_transcriber",
        "hardware": "CPU (CTranslate2 int8)",
        "capabilities": ["audio_to_text"],
        "allowed_tools": ["transcribe_audio"],
        "forbidden_tools": ["*"]
    },
    "gemini_cli": {
        "name": "Gemini CLI / Antigravity",
        "type": "pair_programmer_auditor",
        "capabilities": [
            "system_audit", "code_management", "diagnostics",
            "task_resumption", "unmetered_execution"
        ],
        "allowed_tools": ["read_project_files", "run_tests", "update_handoff", "resume_codex_task"],
        "forbidden_tools": ["cloud_billing", "public_funnel_expose"]
    },
    "codex": {
        "name": "Codex Runtime",
        "type": "execution_environment",
        "capabilities": ["python_runtime", "isolated_environment"],
        "allowed_tools": ["execute_python_scripts"],
        "forbidden_tools": ["arbitrary_cmd_injection"]
    },
    "client_ui": {
        "name": "Cliente UI / PWA",
        "type": "frontend_interface",
        "capabilities": ["user_input", "dashboard_interaction"],
        "allowed_tools": ["chat", "transcribe", "control_devices"],
        "forbidden_tools": ["*"]
    }
}

class MultiAgentBus:
    def __init__(self):
        from pathlib import Path
        self.data_file = Path("C:/Yedid-Smart-Home/data/multiagent_tasks.json")
        self._tasks: Dict[str, Dict[str, Any]] = self._load_tasks()

    def _load_tasks(self) -> Dict[str, Dict[str, Any]]:
        """Carga las tareas guardadas desde el archivo persistente."""
        try:
            if self.data_file.exists():
                import json
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_tasks(self) -> None:
        """Persiste las tareas en disco."""
        try:
            import json
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self._tasks, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_agents(self) -> Dict[str, Any]:
        """Devuelve el inventario de agentes activos y sus permisos."""
        return AGENT_REGISTRY

    def create_task(
        self,
        title: str,
        description: str,
        target_agent: str,
        source_agent: str = "chatgpt_yedid",
        parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Crea y registra una tarea coordinada entre agentes."""
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        now = time.time()

        if target_agent not in AGENT_REGISTRY:
            err = f"Agente '{target_agent}' no registrado en el bus. Disponibles: {list(AGENT_REGISTRY.keys())}"
            log_event("agent_task_creation_rejected", {"title": title, "target": target_agent, "reason": err})
            return {"success": False, "error": err}

        task_record = {
            "task_id": task_id,
            "title": title,
            "description": description,
            "source_agent": source_agent,
            "target_agent": target_agent,
            "parameters": parameters or {},
            "status": "QUEUED",
            "created_at": now,
            "created_iso": datetime.now(timezone.utc).isoformat(),
            "updated_at": now,
            "result_summary": None
        }

        self._tasks[task_id] = task_record
        self._save_tasks()
        log_event("agent_task_created", {
            "task_id": task_id,
            "title": title,
            "source": source_agent,
            "target": target_agent
        })
        return {"success": True, "task": task_record}

    def dispatch_task(
        self,
        source_agent: str,
        target_agent: str,
        title: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Despacha y registra una tarea en el bus multiagente."""
        return self.create_task(
            title=title,
            description=f"Tarea {title} despachada desde {source_agent} hacia {target_agent}",
            target_agent=target_agent,
            source_agent=source_agent,
            parameters=parameters
        )

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Consulta el estado de una tarea por su ID."""
        return self._tasks.get(task_id)

    def update_task_status(self, task_id: str, new_status: str, result_summary: Optional[str] = None) -> bool:
        """Actualiza el estado de ejecución de una tarea."""
        if task_id not in self._tasks:
            return False

        valid_statuses = ["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
        clean_status = new_status.upper().strip()
        if clean_status not in valid_statuses:
            return False

        self._tasks[task_id]["status"] = clean_status
        self._tasks[task_id]["updated_at"] = time.time()
        if result_summary is not None:
            self._tasks[task_id]["result_summary"] = result_summary

        self._save_tasks()
        log_event("agent_task_status_updated", {
            "task_id": task_id,
            "status": clean_status,
            "result": result_summary
        })
        return True

    def list_tasks(self, status_filter: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Lista tareas recientes con filtro opcional de estado."""
        tasks = list(self._tasks.values())
        if status_filter:
            sf = status_filter.upper().strip()
            tasks = [t for t in tasks if t.get("status") == sf]
        return tasks[-limit:]

    def get_recent_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Obtiene las tareas más recientes registradas en el bus."""
        return self.list_tasks(limit=limit)

    async def execute_task(self, task_id: str, timeout: float = 30.0) -> Dict[str, Any]:
        """Ejecuta una tarea delegada a un agente local, llamando al motor real (e.g. Ollama)."""
        import asyncio
        import json
        import urllib.request
        from .config import OLLAMA_URL, OLLAMA_MODEL

        task = self.get_task(task_id)
        if not task:
            return {"success": False, "error": f"Tarea '{task_id}' no encontrada."}

        target = task.get("target_agent")
        prompt = task.get("description") or task.get("title")

        if target in ("ollama_qwen3", "ollama"):
            self.update_task_status(task_id, "RUNNING")
            t0 = time.time()
            try:
                from .resource_manager import resource_manager
                async with resource_manager.track_task(OLLAMA_MODEL):
                    payload = json.dumps({
                        "model": OLLAMA_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "keep_alive": resource_manager.get_current_keep_alive()
                    }).encode("utf-8")

                    req = urllib.request.Request(
                        f"{OLLAMA_URL}/api/generate",
                        data=payload,
                        headers={"Content-Type": "application/json"}
                    )

                    def _call():
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            return json.loads(resp.read().decode())

                    resp_data = await asyncio.to_thread(_call)
                    ans = resp_data.get("response", "").strip()
                    duration = round((time.time() - t0) * 1000, 1)

                self.update_task_status(task_id, "COMPLETED", result_summary=ans)
                task_updated = self.get_task(task_id)
                log_event("agent_task_executed_ollama", {
                    "task_id": task_id,
                    "duration_ms": duration,
                    "response_chars": len(ans)
                })
                return {
                    "success": True,
                    "task": task_updated,
                    "execution": {
                        "agent": "ollama_qwen3",
                        "model": OLLAMA_MODEL,
                        "duration_ms": duration,
                        "output": ans
                    }
                }
            except Exception as e:
                err_msg = f"Error ejecutando tarea en Ollama: {str(e)}"
                self.update_task_status(task_id, "FAILED", result_summary=err_msg)
                return {"success": False, "task_id": task_id, "error": err_msg}
        else:
            return {"success": False, "error": f"Ejecución directa no implementada para el agente '{target}'. Disponibles para ejecución: ['ollama_qwen3']"}

multiagent_bus = MultiAgentBus()
