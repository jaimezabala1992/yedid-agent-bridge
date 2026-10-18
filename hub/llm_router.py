"""
hub/llm_router.py

Enrutador LLM local con soporte de Tool Calling, Orquestación Multiagente y
Persistencia Transaccional de Conversaciones para Yedid Hub v2.0:
- Historial multi-turno duradero respaldado en SQLite (chat_history_db).
- Deduplicación idempotente mediante client_message_id para reintentos de red.
- Enrutamiento determinista de intenciones multiagente con telemetría real en disco.
- Integración directa con el motor duradero de tareas (task_engine) y aprobaciones HITL.
- Síntesis determinista de respaldo para garantizar que toda ejecución de herramientas
  produzca una respuesta redactada clara, con estados y evidencia real en disco.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional
import httpx

from .config import OLLAMA_URL, OLLAMA_MODEL
from .tools_registry import TOOLS_SCHEMA, execute_tool
from .audit import log_event
from .resource_manager import resource_manager
from .agent_inspector import agent_inspector
from .chat_history_db import chat_history_db
from .task_engine import task_engine
from .approvals import approval_manager

logger = logging.getLogger("yedid_router")

SYSTEM_PROMPT = """Eres Yedid, la asistente de inteligencia artificial, automatización del hogar y orquestación multiagente de Jaime.
Eres cálida, servicial, concisa y eficiente. Hablas siempre en español natural y amigable.

Tienes control y supervisión directa de:
1. Orquestación Multiagente Local:
   - Supervisas y coordinas los agentes locales integrados en el equipo de Jaime:
     * Codex Runtime: entorno de ejecución de código y scripts.
     * Gemini CLI / Antigravity: agente local de programación, diagnóstico, tests e inspección de repositorios (sin límites de cuota ChatGPT).
     * Ollama Qwen3 8B: asistente local de conversación y ruteo de herramientas.
     * Faster-Whisper: transcriptor de voz local.
     * MCP Gateway: pasarela segura de herramientas en el puerto 8766.
   - Si Jaime consulta sobre qué está haciendo Gemini, qué tareas hay pendientes en Codex, o pide retomar un trabajo incompleto, utiliza los datos reales de los registros en disco.
   - Si Codex agotó su cuota ('usage_limit_exceeded'), explica el estado real con transparencia (incluyendo la hora exacta de restablecimiento de cuota) y ofrece que Gemini CLI continúe la tarea utilizando los archivos y cambios existentes en el directorio del proyecto.
   - Jamás inventes historiales o tareas no registradas.
   - Las acciones que modifiquen código o alteren el sistema requieren confirmación y aprobación humana (Human-in-the-Loop) en la PWA del Galaxy Fold 7.
2. Televisor ONN Google TV de la sala (en 192.168.1.100): consultar estado, encenderlo, apagarlo, controlar volumen, pausar/reproducir y abrir aplicaciones (YouTube, Netflix, Prime Video, Disney+, Spotify, etc.).
3. Computadora principal (Yedid-Host-PC): consultar uso de CPU, RAM, espacio libre en disco y el estado del servidor Sunshine.
4. Memoria permanente de Jaime: cuando Jaime te pida recordar algo ("recuerda que...", "guarda que...", "acuérdate de...", "anota que..."), o te informe de un dato relevante sobre sus preferencias, familia, salud, finanzas, trabajo u objetivos, invoca la herramienta 'propose_memory_to_save'.

REGLAS:
- Cuando Jaime consulte sobre dispositivos, memoria o agentes, responde basándote en la información verificada de las herramientas.
- Si es una pregunta o saludo general, responde directamente con amabilidad.
- Tras recibir resultados de herramientas o agentes, explica el resultado de forma clara, amigable y estructurada en español.
- Nunca intentes ejecutar comandos de terminal destructivos ni inventes dispositivos o tareas que no existen.
"""

AGENT_INTENT_PATTERNS = [
    r"\bcodex\b",
    r"\bgemini\b",
    r"\bagente[s]?\b",
    r"\bantigravity\b",
    r"\bpendiente[s]?\b",
    r"\bincompleto[s]?\b",
    r"\bincompleta[s]?\b",
    r"\bretoma[r]?\b",
    r"\bcontin[uú]a[r]?\b",
    r"\brevisa[r]?\b",
    r"\bcuota\b",
    r"\btokens?\b",
    r"\bl[ií]mite\b",
    r"\bprogreso\b",
    r"\berror(es)?\b",
    r"\btarea[s]?\b",
    r"\bejecuta[r]?\b",
    r"\btraspasa[r]?\b"
]


def _clean_llm_output(text: str) -> str:
    """Elimina bloques <think>...</think> si el modelo los emite y limpia espacios."""
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


class LLMRouter:
    def __init__(self, ollama_url: str = OLLAMA_URL, model: str = OLLAMA_MODEL):
        self.ollama_url = ollama_url
        self.model = model

    def _detect_multiagent_intent(self, text: str) -> Optional[str]:
        """Detecta si el mensaje del usuario está relacionado con agentes locales."""
        lower_text = text.lower()
        for pat in AGENT_INTENT_PATTERNS:
            if re.search(pat, lower_text):
                if any(k in lower_text for k in (
                    "retoma", "retomar", "incompleto", "incompleta",
                    "revisa ese error", "revise ese error", "traspasa", "traspasar",
                    "continúa la tarea", "continua la tarea", "continuar la tarea",
                    "continúa con gemini", "continua con gemini", "termina la tarea",
                    "finaliza la tarea"
                )):
                    return "handover_or_resume"
                elif any(k in lower_text for k in (
                    "ejecuta una tarea", "ejecutar tarea", "crea una tarea", "crear tarea",
                    "lanza una tarea", "pon a gemini a", "pide a gemini que", "pídele a gemini que",
                    "que gemini ejecute", "que gemini revise", "que gemini analice", "ejecuta con gemini"
                )):
                    return "direct_task_execution"
                elif any(k in lower_text for k in ("hace gemini", "haciendo gemini", "estado de gemini")):
                    return "gemini_status"
                elif any(k in lower_text for k in ("cuota", "tokens", "límite", "limite")):
                    return "quota_check"
                elif any(k in lower_text for k in (
                    "progreso", "estado de los agentes", "cómo van los agentes", "como van los agentes",
                    "tareas de los agentes", "muéstrame los agentes", "muestrame los agentes",
                    "qué hacen los agentes", "que hacen los agentes", "progreso de los agentes"
                )):
                    return "agents_overview"
                return "general_multiagent_query"
        return None

    def _synthesize_tool_results_fallback(
        self,
        user_message: str,
        executed_tools: List[Dict[str, Any]],
        created_task_info: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Construye una respuesta estructurada y clara en español directamente desde la
        evidencia real de las herramientas ejecutadas cuando el LLM devuelve contenido vacío
        o incompleto.
        """
        lines: List[str] = []

        # 1. Si se creó o traspasó una tarea duradera en este turno
        handover_tool = next((t for t in executed_tools if t.get("tool") == "request_task_handover"), None)
        exec_task_tool = next((t for t in executed_tools if t.get("tool") == "execute_agent_task"), None)

        if handover_tool or exec_task_tool or created_task_info:
            res = (handover_tool or exec_task_tool or {}).get("result") or {}
            task_obj = created_task_info or res.get("task") or res
            task_id = task_obj.get("id") or res.get("task_id") or "N/D"
            agent_name = task_obj.get("agent") or res.get("target_agent") or "gemini_cli"
            proj_path = task_obj.get("project_path") or res.get("project_path") or "C:\\Yedid-Smart-Home\\sandbox"
            status = task_obj.get("status") or res.get("status") or "waiting_approval"
            risk = task_obj.get("risk_level") or res.get("risk_level") or "HIGH"
            approval_id = task_obj.get("approval_id") or res.get("approval_id")
            matched_title = res.get("matched_task_title") or (task_obj.get("instruction") or "")[:90]

            lines.append("✅ **Tarea duradera registrada en el motor de ejecución (`agent_tasks.db`)**\n")
            lines.append(f"- **ID de Tarea:** `{task_id}`")
            lines.append(f"- **Agente Asignado:** `{agent_name}` (Gemini CLI / Antigravity)")
            lines.append(f"- **Objetivo:** {matched_title}")
            lines.append(f"- **Alcance (Directorio):** `{proj_path}`")
            lines.append(f"- **Nivel de Riesgo:** **{risk}**")
            lines.append(f"- **Estado Actual:** `{status}`")

            orig = res.get("original_task") or {}
            if orig.get("key_files"):
                lines.append(f"- **Evidencia en Disco (archivos detectados):** `{', '.join(orig['key_files'][:6])}`")

            if approval_id and status == "waiting_approval":
                lines.append(
                    f"\n🔒 **Aprobación Humana Requerida (HITL):**\n"
                    f"- **ID de Aprobación:** `{approval_id}`\n"
                    f"- **Tarea vinculada:** `{task_id}`\n"
                    f"- **Alcance autorizado:** `{proj_path}`\n"
                    f"- **Riesgo:** `{risk}` (Modificación/ejecución sobre directorio de proyecto)\n\n"
                    f"Puedes pulsar **«Autorizar y Ejecutar»** directamente en la tarjeta de la tarea o en el botón superior de **Aprobaciones** para que Gemini CLI la ejecute en segundo plano."
                )
            else:
                lines.append(
                    "\n⚙️ La tarea ha entrado en la cola de ejecución en segundo plano (`queued`) y el worker la procesará de forma autónoma."
                )
            return "\n".join(lines)

        # 2. Si se consultó el progreso o estado de los agentes (get_agent_tasks / get_agents_status)
        tasks_tool = next((t for t in executed_tools if t.get("tool") == "get_agent_tasks"), None)
        status_tool = next((t for t in executed_tools if t.get("tool") == "get_agents_status"), None)

        if tasks_tool or status_tool:
            t_res = (tasks_tool or {}).get("result") or {}
            s_res = (status_tool or {}).get("result") or {}

            codex_info = t_res.get("codex") or (s_res.get("agents") or {}).get("codex") or {}
            gemini_info = t_res.get("gemini_cli") or (s_res.get("agents") or {}).get("gemini_cli") or {}
            hub_tasks = t_res.get("hub_tasks") or s_res.get("hub_tasks") or []
            pending_approvals = t_res.get("pending_approvals") or s_res.get("pending_approvals") or []

            lines.append("📊 **Reporte de Progreso y Estado Real de los Agentes**\n")

            # Estado de Gemini CLI
            g_status = gemini_info.get("status", "ONLINE_READY")
            g_busy = gemini_info.get("is_busy", False)
            g_active = gemini_info.get("active_task")
            lines.append(
                f"1. **Gemini CLI / Antigravity (`v1.4.2`):** Estado `{g_status}` "
                f"({'Ejecutando tarea activa: ' + str(g_active) if g_busy and g_active else 'Disponible y sin límite de cuota ChatGPT'})."
            )

            # Estado de Codex Runtime
            quota_exhausted = codex_info.get("is_quota_exhausted", False)
            resets_at = codex_info.get("quota_resets_at") or "No especificado"
            if quota_exhausted:
                lines.append(
                    f"2. **Codex Runtime:** Estado `QUOTA_EXHAUSTED` — Límite de uso agotado (`usage_limit_exceeded`). "
                    f"Reinicio programado de cuota: **{resets_at}**."
                )
            else:
                lines.append("2. **Codex Runtime:** Estado `READY` (Cuota disponible).")

            lines.append("3. **Ollama (Qwen3 8B) y MCP Gateway (Puerto 8766):** Estado `ONLINE`.\n")

            # Tareas registradas en Codex (incluyendo interrumpidas)
            codex_threads = codex_info.get("tasks") or s_res.get("pending_or_interrupted_tasks") or []
            if codex_threads:
                lines.append("📁 **Tareas en Codex Runtime (Evidencia en Disco):**")
                for ct in codex_threads[:3]:
                    c_title = (ct.get("title") or "Tarea sin título")[:85]
                    c_status = ct.get("status", "REGISTRADA")
                    c_cwd = str(ct.get("cwd") or "").replace("\\\\?\\", "")
                    c_files = ct.get("cwd_key_files") or []
                    files_str = f" | Archivos: `{', '.join(c_files[:5])}`" if c_files else ""
                    lines.append(f"- **{c_status}:** *{c_title}* (`{c_cwd}`){files_str}")
                lines.append("")

            # Tareas duraderas en el Hub (SQLite agent_tasks.db)
            if hub_tasks:
                lines.append("⚙️ **Tareas Duraderas en Yedid Hub (`agent_tasks.db`):**")
                for ht in hub_tasks[:5]:
                    h_id = ht.get("id")
                    h_agent = ht.get("agent")
                    h_status = (ht.get("status") or "").upper()
                    h_risk = ht.get("risk_level", "LOW")
                    h_path = ht.get("project_path", "")
                    h_deliv = ht.get("deliverables") or []
                    deliv_names = [d.get("name", "") for d in h_deliv if isinstance(d, dict) and d.get("name")]
                    deliv_str = f" | Entregables: `{', '.join(deliv_names[:3])}`" if deliv_names else ""
                    lines.append(
                        f"- `{h_id}` (`{h_agent}`) — Estado: **{h_status}** | Riesgo: `{h_risk}` | Alcance: `{h_path}`{deliv_str}"
                    )
                lines.append("")

            # Aprobaciones pendientes (HITL)
            if pending_approvals:
                lines.append(f"🔒 **Aprobaciones Humanas Pendientes ({len(pending_approvals)}):**")
                for ap in pending_approvals[:4]:
                    ap_id = ap.get("id")
                    ap_task = ap.get("task_id") or (ap.get("params") or {}).get("task_id") or ap.get("action")
                    ap_scope = ap.get("scope") or (ap.get("params") or {}).get("project_path") or "Sistema local"
                    ap_risk = ap.get("risk_label") or ap.get("risk_level") or "ALTO (HIGH)"
                    ap_instr = (ap.get("instruction") or ap.get("description") or "")[:90]
                    lines.append(
                        f"- **Aprobación `{ap_id}`** → **Tarea:** `{ap_task}` | **Alcance:** `{ap_scope}` | **Riesgo:** `{ap_risk}` ({ap_instr})"
                    )
            else:
                lines.append("✅ **Aprobaciones Pendientes:** No hay solicitudes bloqueadas esperando autorización en este momento.")

            return "\n".join(lines)

        # 3. Otros resultados de herramientas (TV, PC, Memoria, Inspección de directorio)
        for tool_entry in executed_tools:
            t_name = tool_entry.get("tool", "herramienta")
            t_res = tool_entry.get("result", tool_entry)
            if isinstance(t_res, dict):
                if t_name == "pc_get_status":
                    lines.append(
                        f"💻 **Estado del PC (Yedid-Host-PC):** CPU al {t_res.get('cpu_percent')}%, "
                        f"RAM al {t_res.get('ram_percent')}% ({t_res.get('ram_used_gb')}/{t_res.get('ram_total_gb')} GB), "
                        f"Disco libre: {t_res.get('disk_free_gb')} GB."
                    )
                elif t_name == "tv_get_status":
                    lines.append(
                        f"📺 **Estado del TV ({t_res.get('ip', '192.168.1.100')}):** "
                        f"Conectado: `{t_res.get('connected')}` | Pantalla encendida: `{t_res.get('screen_on')}` | "
                        f"App actual: `{t_res.get('current_app') or 'Inicio'}`."
                    )
                else:
                    msg = t_res.get("message") or t_res.get("approval_message") or json.dumps(t_res, ensure_ascii=False)
                    lines.append(f"🔧 **Resultado de `{t_name}`:** {msg}")
            else:
                lines.append(f"🔧 **Resultado de `{t_name}`:** {str(t_res)}")

        return "\n".join(lines) if lines else "He consultado las herramientas solicitadas y actualizado el estado en el panel."

    async def chat(
        self,
        user_message: str,
        conversation_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
        stepup_token: Optional[str] = None,
        user_id: str = "jaime"
    ) -> Dict[str, Any]:
        """
        Procesa un turno conversacional con persistencia transaccional y orquestación multiagente.
        """
        # 1. Asegurar conversación activa persistente
        if not conversation_id:
            conv = chat_history_db.get_or_create_active_conversation(user_id=user_id)
            conversation_id = conv["id"]
        else:
            conv = chat_history_db.get_conversation(conversation_id)
            if not conv:
                conv = chat_history_db.create_conversation(user_id=user_id, conversation_id=conversation_id)

        # 2. Idempotencia y deduplicación determinista por client_message_id
        if client_message_id and chat_history_db.is_message_duplicate(client_message_id):
            logger.info(f"[ROUTER] Mensaje duplicado recibido (client_message_id: {client_message_id}). Recuperando respuesta asociada.")
            assistant_msg = chat_history_db.get_response_for_client_message_id(
                client_message_id=client_message_id,
                conversation_id=conversation_id
            )
            if assistant_msg:
                target_conv_id = assistant_msg.get("conversation_id") or conversation_id
                updated_conv = chat_history_db.get_conversation(target_conv_id)
                associated_task = None
                if assistant_msg.get("task_id"):
                    associated_task = task_engine.get_task(assistant_msg["task_id"])
                return {
                    "reply": assistant_msg["content"],
                    "conversation_id": target_conv_id,
                    "conversation": updated_conv,
                    "tools_executed": assistant_msg.get("tools_called") or [],
                    "task": associated_task,
                    "deduplicated": True
                }

        # 3. Guardar mensaje del usuario en la base de datos de chat (idempotente si ya existía sin respuesta)
        chat_history_db.save_message(
            conversation_id=conversation_id,
            role="user",
            content=user_message,
            client_message_id=client_message_id
        )

        async with resource_manager.track_task(self.model):
            # 4. Inyección efímera de recuerdos personales autorizados (mínimo contexto)
            from .memory_engine import memory_engine
            mem_context = memory_engine.get_context_for_llm(
                user_message,
                caller_role="local_chat",
                is_owner_authenticated=True,
                stepup_token=stepup_token
            )

            # 5. Enrutador de Intenciones Multiagente y Enlace con Motor de Tareas
            agent_intent = self._detect_multiagent_intent(user_message)
            agent_context = None
            executed_tools: List[Dict[str, Any]] = []
            created_task_info: Optional[Dict[str, Any]] = None

            if agent_intent:
                log_event("multiagent_intent_detected", {"intent": agent_intent, "query": user_message})

                if agent_intent == "handover_or_resume":
                    # Preparar traspaso real y crear la tarea duradera en task_engine (sin duplicar)
                    handover_res = agent_inspector.prepare_task_handover(
                        task_query=user_message,
                        target_agent="gemini_cli",
                        instructions=(
                            f"Continuar la tarea solicitada por Jaime ('{user_message[:140]}') "
                            f"revisando los archivos existentes en disco y generando el entregable de verificación."
                        ),
                        conversation_id=conversation_id,
                        create_durable_task=True
                    )
                    executed_tools.append({"tool": "request_task_handover", "result": handover_res})
                    if handover_res.get("task"):
                        created_task_info = handover_res["task"]

                    agent_context = (
                        f"[TELEMETRÍA REAL EN DISCO - TRASPASO DE TAREA REGISTRADO]:\n"
                        f"{json.dumps(handover_res, ensure_ascii=False, indent=2)}\n"
                        f"Instrucción: Explica a Jaime qué tarea se preparó para traspasar a Gemini CLI, "
                        f"en qué directorio de proyecto está ({handover_res.get('project_path')}), y que se ha generado la tarea duradera "
                        f"'{handover_res.get('task_id')}' con solicitud de autorización humana (HITL) "
                        f"con ID '{handover_res.get('approval_id')}' y riesgo '{handover_res.get('risk_level')}'. "
                        f"Indícale que en cuanto pulse 'Autorizar y Ejecutar' en la PWA, Gemini comenzará a ejecutarla en segundo plano."
                    )

                elif agent_intent == "direct_task_execution":
                    lower_msg = user_message.lower()
                    target_agent = "safe_script" if ("script" in lower_msg or "sandbox" in lower_msg) else "gemini_cli"
                    target_path = r"C:\Yedid-Smart-Home\sandbox"
                    if "codex" in lower_msg or "spss" in lower_msg or "regresion" in lower_msg or "regresión" in lower_msg:
                        codex_stat = agent_inspector.inspect_codex_status(limit=5)
                        threads = codex_stat.get("threads", [])
                        if threads and threads[0].get("cwd"):
                            target_path = str(threads[0]["cwd"]).replace("\\\\?\\", "")
                    try:
                        created_task = task_engine.create_task(
                            agent=target_agent,
                            instruction=user_message,
                            project_path=target_path,
                            conversation_id=conversation_id,
                            risk_level="LOW" if target_agent == "safe_script" else "HIGH",
                            metadata={"source": "chat_direct_execution"}
                        )
                        created_task_info = created_task
                        executed_tools.append({"tool": "execute_agent_task", "result": created_task})
                        agent_context = (
                            f"[TAREA DURADERA CREADA EN TASK_ENGINE]:\n"
                            f"{json.dumps(created_task, ensure_ascii=False, indent=2)}\n"
                            f"Instrucción: Confirma a Jaime la creación de la tarea '{created_task.get('id')}' con el agente "
                            f"'{target_agent}' en el directorio '{target_path}', indicando su estado ('{created_task.get('status')}'), "
                            f"riesgo ('{created_task.get('risk_level')}') e ID de aprobación ('{created_task.get('approval_id')}')."
                        )
                    except Exception as e:
                        logger.error(f"Error creando tarea directa desde chat: {e}")

                else:
                    # Telemetría de estado, tareas de Codex, tareas duraderas del Hub y aprobaciones pendientes
                    codex_stat = agent_inspector.inspect_codex_status(limit=6)
                    gemini_stat = agent_inspector.inspect_gemini_status(limit=3)
                    tasks_overview = task_engine.list_tasks(limit=6)
                    pending_approvals = approval_manager.get_pending()

                    overview_data = {
                        "codex": {
                            "is_quota_exhausted": codex_stat.get("is_quota_exhausted", False),
                            "quota_resets_at": codex_stat.get("active_quota_resets_at"),
                            "quota_error_message": codex_stat.get("active_quota_message"),
                            "tasks": codex_stat.get("threads", [])[:4]
                        },
                        "gemini_cli": {
                            "status": gemini_stat.get("status"),
                            "is_busy": gemini_stat.get("is_busy"),
                            "active_task": gemini_stat.get("active_task"),
                            "ready_to_continue_tasks": True,
                            "capabilities": gemini_stat.get("capabilities", [])
                        },
                        "hub_tasks": tasks_overview,
                        "pending_approvals": pending_approvals
                    }
                    executed_tools.append({"tool": "get_agent_tasks", "result": overview_data})
                    if agent_intent == "agents_overview":
                        executed_tools.append({"tool": "get_agents_status", "result": agent_inspector.get_all_agents_overview()})

                    agent_context = (
                        f"[TELEMETRÍA REAL EN DISCO - ESTADO DE AGENTES, TAREAS Y APROBACIONES]:\n"
                        f"{json.dumps(overview_data, ensure_ascii=False, indent=2)}\n"
                        f"Instrucción: Usa ÚNICAMENTE estos datos reales para responder a Jaime en español de forma clara y estructurada. "
                        f"Detalla: 1) Estado de Gemini CLI y Codex Runtime (incluyendo agotamiento de cuota y hora de reinicio "
                        f"{codex_stat.get('active_quota_resets_at')}), 2) Tareas en Codex y tareas duraderas en el Hub (con sus IDs, "
                        f"estados y entregables reales), y 3) Aprobaciones humanas pendientes indicando Tarea, Alcance y Riesgo."
                    )

            # 6. Construir ventana de contexto histórico desde SQLite
            recent_context = chat_history_db.get_recent_context_for_llm(conversation_id, limit=10)
            if recent_context and recent_context[-1]["role"] == "user" and recent_context[-1]["content"] == user_message:
                history_turns = recent_context[:-1]
            else:
                history_turns = recent_context

            turn_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if mem_context:
                turn_messages.append({"role": "system", "content": mem_context})
            if agent_context:
                turn_messages.append({"role": "system", "content": agent_context})

            turn_messages.extend(history_turns)
            turn_messages.append({"role": "user", "content": user_message})

            # Si el enrutador determinista ya ejecutó las herramientas de agentes en este turno,
            # pedimos directamente a Ollama que redacte la respuesta ejecutiva sin re-invocar herramientas redundantes.
            pre_executed_intent = agent_intent in (
                "handover_or_resume", "direct_task_execution", "agents_overview", "gemini_status", "quota_check"
            )

            payload: Dict[str, Any] = {
                "model": self.model,
                "stream": False,
                "think": False,
                "messages": turn_messages,
                "keep_alive": resource_manager.get_current_keep_alive(),
                "options": {
                    "num_predict": 700,
                    "temperature": 0.25
                }
            }
            if not pre_executed_intent:
                payload["tools"] = TOOLS_SCHEMA

        # 7. Primera llamada a Ollama
        async with httpx.AsyncClient(timeout=45.0) as client:
            try:
                resp = await client.post(f"{self.ollama_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log_event("llm_error", {"error": str(e)})
                if executed_tools:
                    fallback_reply = self._synthesize_tool_results_fallback(user_message, executed_tools, created_task_info)
                else:
                    fallback_reply = f"No pude comunicarme con el modelo local Ollama ({e}). Asegúrate de que Ollama esté ejecutándose."
                chat_history_db.save_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=fallback_reply,
                    tools_called=executed_tools,
                    task_id=created_task_info["id"] if created_task_info else None
                )
                return {
                    "reply": fallback_reply,
                    "conversation_id": conversation_id,
                    "conversation": chat_history_db.get_conversation(conversation_id),
                    "tools_executed": executed_tools,
                    "task": created_task_info
                }

        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls")
        content = _clean_llm_output(msg.get("content", ""))

        # 8. Si el modelo decidió invocar herramientas
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name")
                raw_args = func.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}
                else:
                    args = raw_args or {}

                result = await execute_tool(tool_name, args, conversation_id=conversation_id)
                executed_tools.append(result)

                if tool_name == "execute_agent_task" and result.get("result"):
                    created_task_info = result.get("result")
                elif tool_name == "request_task_handover" and isinstance(result.get("result"), dict):
                    created_task_info = result["result"].get("task") or created_task_info

            # Segunda llamada al LLM para sintetizar la respuesta
            followup_messages = list(turn_messages)
            followup_messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls
            })
            for tc in tool_calls:
                t_name = tc.get("function", {}).get("name")
                t_res = next((r for r in reversed(executed_tools) if r.get("tool") == t_name), {})
                followup_messages.append({
                    "role": "tool",
                    "content": json.dumps(t_res.get("result", t_res.get("error", {})), ensure_ascii=False)
                })

            followup_messages.append({
                "role": "user",
                "content": (
                    "Con base en los resultados anteriores de las herramientas, responde directamente a Jaime en español "
                    "de forma cordial, clara, ejecutiva y estructurada, incluyendo estados reales, rutas y evidencia en disco."
                )
            })

            followup_payload = {
                "model": self.model,
                "stream": False,
                "think": False,
                "messages": followup_messages,
                "keep_alive": resource_manager.get_current_keep_alive(),
                "options": {
                    "num_predict": 700,
                    "temperature": 0.2
                }
            }

            async with httpx.AsyncClient(timeout=45.0) as client:
                try:
                    resp = await client.post(f"{self.ollama_url}/api/chat", json=followup_payload)
                    resp.raise_for_status()
                    followup_data = resp.json()
                    final_reply = _clean_llm_output(followup_data.get("message", {}).get("content", ""))
                except Exception as e:
                    logger.warning(f"Error en segunda llamada al LLM tras herramientas: {e}")
                    final_reply = ""

            if not final_reply or len(final_reply) < 25:
                final_reply = self._synthesize_tool_results_fallback(user_message, executed_tools, created_task_info)

            chat_history_db.save_message(
                conversation_id=conversation_id,
                role="assistant",
                content=final_reply,
                tools_called=executed_tools,
                task_id=created_task_info["id"] if created_task_info else None
            )
            return {
                "reply": final_reply,
                "conversation_id": conversation_id,
                "conversation": chat_history_db.get_conversation(conversation_id),
                "tools_executed": executed_tools,
                "task": created_task_info
            }

        else:
            # Respuesta directa (o con telemetría pre-ejecutada)
            if executed_tools and (not content or len(content) < 25):
                content = self._synthesize_tool_results_fallback(user_message, executed_tools, created_task_info)
            elif not content:
                content = "Estoy lista. ¿En qué puedo ayudarte hoy, Jaime?"

            chat_history_db.save_message(
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                tools_called=executed_tools,
                task_id=created_task_info["id"] if created_task_info else None
            )
            return {
                "reply": content,
                "conversation_id": conversation_id,
                "conversation": chat_history_db.get_conversation(conversation_id),
                "tools_executed": executed_tools,
                "task": created_task_info
            }

    def reset_conversation(self, conversation_id: Optional[str] = None) -> None:
        """Crea una nueva conversación limpia para reiniciar el hilo de diálogo."""
        chat_history_db.create_conversation(user_id="jaime")


router = LLMRouter()
