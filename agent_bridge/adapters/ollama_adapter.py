# -*- coding: utf-8 -*-
"""
agent_bridge/adapters/ollama_adapter.py

Adaptador para Ollama local (Qwen3 8B).
- Reutiliza el Resource Manager existente en el Hub.
- Cero instancias duplicadas de Ollama.
- Desalojo automático de memoria tras inactividad.
"""

import time
import logging
from typing import Dict, Any, Optional
import httpx
from .base import BaseAgentAdapter
from ..schemas import AgentTask, AgentResult
from ..config import load_bridge_config

logger = logging.getLogger("YedidAgentBridge.OllamaAdapter")

class OllamaAdapter(BaseAgentAdapter):
    """Adaptador para el motor de inferencia local Ollama."""

    def __init__(self, ollama_url: Optional[str] = None, model: Optional[str] = None):
        super().__init__(name="ollama")
        cfg = load_bridge_config()
        self.ollama_url = (ollama_url or cfg.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = model or cfg.get("ollama_model", "qwen3:8b")
        self.timeout = 60.0

    async def is_available(self) -> bool:
        """Verifica que el demonio Ollama esté respondiendo en su puerto local."""
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(f"{self.ollama_url}/api/tags")
                return res.status_code == 200
        except Exception:
            return False

    def _get_keep_alive(self) -> str:
        """Consulta el keep_alive actual del Resource Manager de Yedid Hub si está disponible."""
        try:
            from hub.resource_manager import resource_manager
            return resource_manager.get_current_keep_alive()
        except Exception:
            return "3m"

    async def execute(self, task: AgentTask) -> AgentResult:
        """Ejecuta una instrucción en Ollama reutilizando las políticas de recursos."""
        start_time = time.time()
        task_id = task.task_id

        # Intentar registrar tarea activa en el Resource Manager para prevenir descargas a mitad de proceso
        has_rm = False
        try:
            from hub.resource_manager import resource_manager
            resource_manager.register_active_task(self.model)
            has_rm = True
        except Exception as e:
            logger.debug(f"Resource Manager no cargado directamente: {e}")

        try:
            keep_alive = self._get_keep_alive()
            payload = {
                "model": self.model,
                "prompt": task.instructions,
                "stream": False,
                "keep_alive": keep_alive
            }

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(f"{self.ollama_url}/api/generate", json=payload)

            if res.status_code != 200:
                elapsed = time.time() - start_time
                return AgentResult(
                    task_id=task_id,
                    agent=self.name,
                    status="failed",
                    result=f"Error en Ollama (HTTP {res.status_code}): {res.text}",
                    metadata={"execution_time_sec": round(elapsed, 2)}
                )

            data = res.json()
            raw_response = data.get("response", "").strip()
            elapsed = time.time() - start_time

            return AgentResult(
                task_id=task_id,
                agent=self.name,
                status="completed",
                result=raw_response,
                requires_followup=False,
                metadata={
                    "model": self.model,
                    "execution_time_sec": round(elapsed, 2),
                    "eval_count": data.get("eval_count", 0)
                }
            )

        except httpx.ConnectError:
            return AgentResult(
                task_id=task_id,
                agent=self.name,
                status="failed",
                result=f"No se pudo conectar al demonio local de Ollama en {self.ollama_url}. Verifica que esté activo.",
                metadata={"execution_time_sec": round(time.time() - start_time, 2)}
            )
        except Exception as e:
            logger.error(f"Error ejecutando tarea {task_id} en Ollama: {e}")
            return AgentResult(
                task_id=task_id,
                agent=self.name,
                status="failed",
                result=f"Excepción en adaptador Ollama: {str(e)}",
                metadata={"execution_time_sec": round(time.time() - start_time, 2)}
            )
        finally:
            if has_rm:
                try:
                    from hub.resource_manager import resource_manager
                    resource_manager.finish_active_task(self.model)
                except Exception:
                    pass
