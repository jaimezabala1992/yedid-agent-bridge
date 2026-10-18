# -*- coding: utf-8 -*-
"""
agent_bridge/security.py

Motor de seguridad, validación de remitentes y prevención de fugas para Yedid Agent Bridge.
- Tratamiento del contenido de GitHub Issues como ENTRADA NO CONFIABLE (Untrusted Input).
- Cero terminal remota ni ejecución arbitraria de comandos.
- Aislamiento estricto de memorias personales, claves DPAPI y respaldos.
"""

import re
import logging
from typing import List, Tuple, Dict, Any, Optional
from .schemas import AgentTask

logger = logging.getLogger("YedidAgentBridge.Security")

# Decisiones de política
DECISION_AUTO_ALLOW = "AUTO_ALLOW"
DECISION_REQUIRE_HITL = "REQUIRE_HITL"
DECISION_REJECT = "REJECT"

# Patrones prohibidos en instrucciones (Inyección de comandos, acceso a secretos, borrados)
FORBIDDEN_INSTRUCTION_PATTERNS = [
    r"\b(?:rmdir|del\s+/|format\s+[a-z]:|remove-item\s+-recurse)\b",
    r"\b(?:powershell|cmd\.exe|bash|sh|zsh)\s*-[c|e|command]",
    r"\b(?:\.token|\.memory_master\.key|memory\.db|audit\.jsonl)\b",
    r"\b(?:drop\s+table|delete\s+from\s+memories)\b",
    r"\b(?:eval\(|exec\(|subprocess|os\.system|shell_exec)\b",
    r"\b(?:BEGIN\s+PRIVATE\s+KEY|id_rsa|id_ed25519)\b"
]

# Patrones para saneamiento de salida (prevenir fugas de tokens o datos sensibles a GitHub)
REDACTION_PATTERNS = [
    (r"ghp_[a-zA-Z0-9]{36,}", "[GITHUB_PAT_REDACTED]"),
    (r"github_pat_[a-zA-Z0-9_]{50,}", "[GITHUB_FINE_GRAINED_PAT_REDACTED]"),
    (r"stepup_[a-f0-9]{32}", "[STEPUP_TOKEN_REDACTED]"),
    (r"session_id=[a-zA-Z0-9_\-]{20,}", "session_id=[REDACTED]"),
    (r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}", "Bearer [REDACTED]"),
    (r"csrf_token=[a-zA-Z0-9_\-]{20,}", "csrf_token=[REDACTED]"),
    (r"AIza[0-9A-Za-z\-_]{35}", "[GEMINI_API_KEY_REDACTED]"),
    (r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----", "[PRIVATE_KEY_REDACTED]")
]

class SecurityPolicyEngine:
    """Evalúa la legitimidad y los límites de seguridad de cada tarea antes de despacharla."""

    def __init__(self, authorized_users: Optional[List[str]] = None):
        self.authorized_users = [u.lower().strip() for u in (authorized_users or ["jaime", "yedid-host"])]

    def is_author_authorized(self, github_username: str) -> bool:
        """Verifica si el autor del Issue en GitHub está explícitamente autorizado."""
        if not github_username:
            return False
        clean = github_username.lower().strip()
        return clean in self.authorized_users

    def evaluate(self, task: AgentTask, author: str) -> Tuple[str, str]:
        """Evalúa una tarea entrante y decide si se ejecuta, requiere HITL o se rechaza.
        
        Retorna:
            (decision, reason)
        """
        # 1. Comprobación de remitente
        if not self.is_author_authorized(author):
            logger.warning(f"Remitente no autorizado en GitHub: '{author}' para task '{task.task_id}'")
            return DECISION_REJECT, f"El usuario de GitHub '@{author}' no está en la lista blanca de operadores autorizados de Yedid."

        # 2. Análisis de patrones prohibidos en instrucciones
        for pat in FORBIDDEN_INSTRUCTION_PATTERNS:
            if re.search(pat, task.instructions, re.IGNORECASE):
                logger.warning(f"Instrucción bloqueada por política de seguridad (patrón '{pat}') en task '{task.task_id}'")
                return DECISION_REJECT, "La solicitud contiene patrones o comandos prohibidos que violan la política de aislamiento de Yedid."

        # 3. Clasificación según agente y operación
        target = task.target_agent.lower()
        op = task.operation.lower().strip()

        # Operaciones inocuas y de solo lectura para Ollama y Hub
        if target == "ollama":
            if op in ("respond", "ping", "summarize", "explain", "translate"):
                return DECISION_AUTO_ALLOW, "Operación estándar de inferencia local autorizada para Ollama."
            return DECISION_REQUIRE_HITL, f"Operación '{op}' en Ollama requiere confirmación previa."

        elif target == "yedid_hub":
            if op in ("status", "health", "vitals", "ping"):
                return DECISION_AUTO_ALLOW, "Consulta de telemetría y estado del sistema autorizada."
            # Cualquier acción mutante sobre dispositivos o escenas requiere HITL
            return DECISION_REQUIRE_HITL, f"Acción mutante '{op}' en el Hub requiere autorización humana."

        elif target in ("gemini", "codex"):
            if op in ("diagnose", "review", "analyze", "propose_fix"):
                # Si la tarea declara expresamente requerir aprobación o involucra modificar archivos
                if task.requires_approval or "modificar" in task.instructions.lower() or "guardar" in task.instructions.lower():
                    return DECISION_REQUIRE_HITL, "Propuesta o diagnóstico con impacto en código requiere aprobación previa."
                return DECISION_AUTO_ALLOW, f"Análisis de solo lectura autorizado para {target}."
            return DECISION_REQUIRE_HITL, f"Operación '{op}' para {target} requiere confirmación de seguridad."

        return DECISION_REJECT, f"Agente '{target}' u operación '{op}' no reconocidos por la política de seguridad."

    @staticmethod
    def sanitize_output(output: str) -> str:
        """Elimina cualquier token, clave o dato sensible de la salida antes de enviarla a GitHub."""
        if not output:
            return ""

        sanitized = output
        for pat, replacement in REDACTION_PATTERNS:
            sanitized = re.sub(pat, replacement, sanitized)

        return sanitized
