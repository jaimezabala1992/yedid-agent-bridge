# -*- coding: utf-8 -*-
"""
agent_bridge/schemas.py

Validación estricta de esquemas de tareas y resultados para Yedid Agent Bridge.
- Esquema de Entrada: yedid-agent-task/1.0
- Esquema de Salida: yedid-agent-result/1.0
"""

import re
import json
import warnings
from typing import Dict, Any, Optional, Literal

warnings.filterwarnings("ignore", message=r".*shadows an attribute in parent.*", category=UserWarning)

from pydantic import BaseModel, Field, field_validator, ConfigDict


TASK_SCHEMA_VERSION = "yedid-agent-task/1.0"
RESULT_SCHEMA_VERSION = "yedid-agent-result/1.0"

ALLOWED_TARGET_AGENTS = ("ollama", "gemini", "codex", "yedid_hub")
ALLOWED_STATUSES = ("queued", "running", "waiting_approval", "completed", "failed", "cancelled")
ALLOWED_PRIORITIES = ("low", "normal", "high")

class AgentTask(BaseModel):
    """Esquema oficial de solicitud enviada desde ChatGPT mediante GitHub Issue."""
    model_config = ConfigDict(protected_namespaces=())

    schema: str = Field(..., description="Identificador del esquema, debe ser 'yedid-agent-task/1.0'")
    task_id: str = Field(..., description="Identificador único alfanumérico de la tarea")
    target_agent: Literal["ollama", "gemini", "codex", "yedid_hub"] = Field(..., description="Agente local destinatario")
    operation: str = Field(..., description="Operación solicitada (ej. respond, diagnose, review, status)")
    priority: Literal["low", "normal", "high"] = Field(default="normal", description="Prioridad de ejecución")
    instructions: str = Field(..., description="Instrucción detallada para el agente")
    requires_approval: bool = Field(default=False, description="Bandera declarativa de requerimiento de aprobación")
    context_hints: Optional[Dict[str, Any]] = Field(default=None, description="Metadatos o pistas de contexto adicionales")

    @field_validator("schema")
    @classmethod
    def validate_schema(cls, v: str) -> str:
        if v.strip() != TASK_SCHEMA_VERSION:
            raise ValueError(f"Esquema no soportado: '{v}'. Se requiere '{TASK_SCHEMA_VERSION}'.")
        return v.strip()

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, v: str) -> str:
        clean = v.strip()
        if not re.match(r"^[a-zA-Z0-9_\-\.]{3,64}$", clean):
            raise ValueError(f"task_id inválido: '{v}'. Debe contener 3-64 caracteres alfanuméricos, guiones o guiones bajos.")
        return clean

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("Las instrucciones no pueden estar vacías.")
        if len(clean) > 50000:
            raise ValueError("Las instrucciones exceden el límite máximo de 50,000 caracteres.")
        return clean

class AgentResult(BaseModel):
    """Esquema oficial de respuesta publicada por el Bridge en el Issue."""
    model_config = ConfigDict(protected_namespaces=())

    schema: str = Field(default=RESULT_SCHEMA_VERSION, description="Identificador del esquema de resultado")
    task_id: str = Field(..., description="Identificador correlacionado de la tarea")
    agent: str = Field(..., description="Agente que procesó la tarea")
    status: Literal["queued", "running", "waiting_approval", "completed", "failed", "cancelled"] = Field(..., description="Estado de la ejecución")
    result: str = Field(..., description="Contenido de la respuesta o mensaje de estado")
    requires_followup: bool = Field(default=False, description="Indica si se requiere seguimiento posterior")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Metadatos técnicos de ejecución")

    def to_github_comment(self) -> str:
        """Formatea el resultado como bloque de código JSON con encabezado legible para ChatGPT."""
        status_icons = {
            "completed": "✅",
            "failed": "❌",
            "waiting_approval": "⏳",
            "running": "⚡",
            "queued": "📥",
            "cancelled": "🚫"
        }
        icon = status_icons.get(self.status, "ℹ️")
        json_str = json.dumps(self.model_dump(), indent=2, ensure_ascii=False)
        
        return (
            f"### {icon} Yedid Agent Bridge — {self.status.upper()}\n\n"
            f"**Task ID:** `{self.task_id}`  \n"
            f"**Agente:** `{self.agent}`  \n"
            f"**Estado:** `{self.status}`\n\n"
            f"```json\n{json_str}\n```\n"
        )

def extract_and_parse_task(text: str) -> Optional[AgentTask]:
    """Extrae y valida una tarea en formato JSON desde el cuerpo de un Issue de GitHub.
    
    Soporta:
    1. JSON crudo.
    2. JSON contenido en bloques markdown ```json ... ```.
    3. JSON detectado entre corchetes { ... }.
    """
    if not text or not text.strip():
        return None

    cleaned = text.strip()

    # Intentar búsqueda de bloque ```json ... ```
    match_code_block = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned, re.IGNORECASE)
    if match_code_block:
        candidate = match_code_block.group(1)
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and data.get("schema") == TASK_SCHEMA_VERSION:
                return AgentTask(**data)
        except Exception:
            pass

    # Intentar parseo directo del texto completo
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and data.get("schema") == TASK_SCHEMA_VERSION:
            return AgentTask(**data)
    except Exception:
        pass

    # Intentar extraer el primer bloque JSON balanceado que contenga el schema
    if TASK_SCHEMA_VERSION in cleaned:
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = cleaned[start_idx:end_idx + 1]
            try:
                data = json.loads(candidate)
                if isinstance(data, dict) and data.get("schema") == TASK_SCHEMA_VERSION:
                    return AgentTask(**data)
            except Exception:
                pass

    return None
