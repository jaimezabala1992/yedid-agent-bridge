# -*- coding: utf-8 -*-
"""
agent_bridge/adapters/base.py

Clase base abstracta para los adaptadores de agentes locales en Yedid.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from ..schemas import AgentTask, AgentResult

class BaseAgentAdapter(ABC):
    """Interfaz estándar que debe implementar cada adaptador de agente."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def is_available(self) -> bool:
        """Comprueba si el agente local está operativo y disponible para recibir tareas."""
        pass

    @abstractmethod
    async def execute(self, task: AgentTask) -> AgentResult:
        """Ejecuta la tarea en el agente local y devuelve el resultado formateado."""
        pass
