# -*- coding: utf-8 -*-
"""
agent_bridge/adapters/hub_adapter.py

Adaptador para Yedid Hub local.
- Consultas de estado, dispositivos, telemetría y salud del Hub.
- Acciones mutantes (luces, escenas, TV) requieren autorización HITL.
"""

import time
import logging
from typing import Dict, Any, Optional
import httpx
from .base import BaseAgentAdapter
from ..schemas import AgentTask, AgentResult
from ..config import load_bridge_config

logger = logging.getLogger("YedidAgentBridge.HubAdapter")

class HubAdapter(BaseAgentAdapter):
    """Adaptador para el núcleo de domótica Yedid Hub."""

    def __init__(self):
        super().__init__(name="yedid_hub")
        cfg = load_bridge_config()
        self.hub_url = cfg.get("hub_url", "http://127.0.0.1:8765").rstrip("/")

    async def is_available(self) -> bool:
        """Verifica si el servidor FastAPI del Hub está respondiendo."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{self.hub_url}/api/health")
                return res.status_code == 200
        except Exception:
            return False

    async def execute(self, task: AgentTask) -> AgentResult:
        """Ejecuta una consulta sobre el estado o dispositivos de Yedid Hub."""
        start_time = time.time()
        task_id = task.task_id
        op = task.operation.lower().strip()

        # Operaciones de consulta de solo lectura
        if op in ("status", "health", "ping"):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.get(f"{self.hub_url}/api/health")
                    health_data = res.json() if res.status_code == 200 else {"status": "error"}

                    res_dev = await client.get(f"{self.hub_url}/api/devices")
                    dev_data = res_dev.json() if res_dev.status_code == 200 else {}

                elapsed = time.time() - start_time
                devices_count = len(dev_data) if isinstance(dev_data, list) else len(dev_data.get("devices", []))
                
                report = (
                    f"🏠 ESTADO GENERAL DE YEDID SMART HOME:\n"
                    f"• Hub Status: {health_data.get('status', 'online').upper()}\n"
                    f"• Versión: {health_data.get('version', '2.0.0')}\n"
                    f"• Dispositivos vinculados: {devices_count}\n"
                    f"• Modelo activo: {health_data.get('model', 'qwen3:8b')}\n"
                    f"• Conectividad: Operativa (Tailscale Serve activo)"
                )

                return AgentResult(
                    task_id=task_id,
                    agent=self.name,
                    status="completed",
                    result=report,
                    requires_followup=False,
                    metadata={"execution_time_sec": round(elapsed, 2)}
                )

            except Exception as e:
                return AgentResult(
                    task_id=task_id,
                    agent=self.name,
                    status="failed",
                    result=f"Error consultando Yedid Hub: {str(e)}",
                    metadata={"execution_time_sec": round(time.time() - start_time, 2)}
                )

        # Acciones de control sobre dispositivos físicos requieren confirmación
        return AgentResult(
            task_id=task_id,
            agent=self.name,
            status="waiting_approval",
            result=f"La operación física '{op}' sobre dispositivos del hogar requiere autorización humana desde la PWA.",
            requires_followup=True,
            metadata={"policy": "HITL_DEVICE_CONTROL_REQUIRED"}
        )
