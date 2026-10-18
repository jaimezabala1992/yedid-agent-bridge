"""Catálogo de herramientas permitidas y ejecutor seguro para Yedid Hub."""
import asyncio
from typing import Any, Dict, List, Optional
from .tv_service import tv_service, ALLOWED_KEYS, APP_SHORTCUTS
from .system_service import get_system_vitals
from .audit import log_event

# Esquemas de herramientas expuestas al LLM (Ollama / OpenAI spec)
TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_tv_status",
            "description": "Consulta el estado actual del televisor ONN Google TV de la sala (si está encendido o apagado, y qué app tiene abierta).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "turn_on_tv",
            "description": "Enciende o saca de reposo el televisor de la sala.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "turn_off_tv",
            "description": "Apaga o pone en reposo el televisor de la sala.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "launch_tv_app",
            "description": "Abre una aplicación en el televisor de la sala. Ejemplos de app_name: 'youtube', 'netflix', 'prime', 'disney', 'max', 'spotify', 'twitch', 'tailscale'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Nombre de la app a abrir, por ejemplo 'youtube' o 'netflix'."
                    }
                },
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_tv_key",
            "description": "Envía una pulsación de tecla o comando al control remoto del televisor. Teclas comunes: 'VOLUME_UP', 'VOLUME_DOWN', 'MUTE', 'PLAY_PAUSE', 'BACK', 'HOME', 'DPAD_CENTER', 'DPAD_UP', 'DPAD_DOWN'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key_name": {
                        "type": "string",
                        "description": "Nombre de la tecla en mayúsculas (ej: PLAY_PAUSE, VOLUME_UP, VOLUME_DOWN, MUTE, HOME, BACK)."
                    }
                },
                "required": ["key_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_pc_vitals",
            "description": "Obtiene el estado de hardware de la computadora principal de Jaime (uso de CPU, memoria RAM libre, espacio en disco C y D, y estado de Sunshine para streaming).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_scene",
            "description": "Ejecuta una escena o rutina automatizada del hogar. Opciones permitidas: 'modo_cine', 'modo_gaming', 'modo_musica', 'modo_noche'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_name": {
                        "type": "string",
                        "description": "Nombre de la escena ('modo_cine', 'modo_gaming', 'modo_musica', 'modo_noche')."
                    }
                },
                "required": ["scene_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_memory_to_save",
            "description": "Propone guardar un recuerdo o dato personal, técnico o del hogar sobre Jaime. IMPORTANTE: Este recuerdo NUNCA se activa directamente; se guarda en la cola de Revisión Humana Pendiente (Human-in-the-Loop) para que Jaime lo confirme y apruebe en la PWA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Título conciso y claro del recuerdo (ej. 'Médico de cabecera', 'Preferencia de café')."
                    },
                    "category": {
                        "type": "string",
                        "description": "Categoría: 'familia', 'salud', 'finanzas', 'trabajo', 'estudios', 'relaciones', 'preferencias', 'proyectos', 'general', 'hogar', 'objetivos', 'intereses', 'tecnica'."
                    },
                    "access_level": {
                        "type": "string",
                        "description": "Nivel de privacidad: 'general', 'personal' (por defecto) o 'muy_privado' (para salud, finanzas o datos confidenciales)."
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenido detallado y completo del recuerdo a preservar."
                    }
                },
                "required": ["title", "category", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_agents_status",
            "description": "Consulta el estado en tiempo real de todos los agentes integrados en Yedid: Ollama (Qwen3 8B), Gemini CLI, Codex, Faster-Whisper y MCP Gateway. Informa si tienen cuota disponible, qué tareas realizan y si están listos.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_tasks",
            "description": "Consulta tareas de los agentes locales (Codex y Gemini CLI). Detecta tareas pendientes, activas o interrumpidas por límite de cuota o error.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Filtro opcional por agente ('codex', 'gemini_cli', 'all')."
                    },
                    "status_filter": {
                        "type": "string",
                        "description": "Filtro opcional ('interrupted', 'pending', 'all')."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_codex_quota",
            "description": "Comprueba específicamente si Codex tiene cuota de tokens disponible o si alcanzó el límite de uso ('usage limit exceeded'). Devuelve la hora exacta de restablecimiento y la última tarea afectada.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_context",
            "description": "Inspecciona el estado de los archivos y git de un proyecto en disco (el repositorio Yedid Smart Home o proyectos de Codex como regresión SPSS, scripts de Moonlight, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_keyword": {
                        "type": "string",
                        "description": "Palabra clave o nombre del proyecto (ej: 'spss', 'regresion', 'moonlight', 'yedid'). Si no se indica, inspecciona el repositorio central Yedid."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "request_task_handover",
            "description": "Coordina el traspaso de una tarea interrumpida en Codex para que sea continuada por Gemini CLI usando los archivos reales del proyecto. Si requiere modificar código, crea automáticamente una solicitud de aprobación humana (HITL) en la PWA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_query": {
                        "type": "string",
                        "description": "Identificador, tema o palabra clave de la tarea a traspasar (ej: 'regresion', 'spss', 'moonlight', 'incompleto')."
                    },
                    "target_agent": {
                        "type": "string",
                        "description": "Agente destino compatible (por defecto 'gemini_cli')."
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Instrucciones específicas para el agente que toma el relevo."
                    }
                },
                "required": ["task_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_agent_task",
            "description": "Crea y encola una tarea de ejecución real con un agente local ('gemini_cli' o 'safe_script'). Si es de ALTO RIESGO (modifica código del sistema), queda en espera de aprobación humana (HITL). Si es de BAJO RIESGO (diagnóstico, pruebas en sandbox), se ejecuta inmediatamente en segundo plano.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agente ejecutor: 'gemini_cli' (Antigravity CLI) o 'safe_script'."
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Instrucción exacta a ejecutar."
                    },
                    "project_path": {
                        "type": "string",
                        "description": "Ruta del proyecto autorizado (ej. 'C:\\Yedid-Smart-Home' o '%USERPROFILE%\\Documents\\Codex')."
                    },
                    "risk_level": {
                        "type": "string",
                        "description": "'LOW' para pruebas/diagnósticos no destructivos, 'HIGH' para modificaciones de código (requiere aprobación HITL)."
                    }
                },
                "required": ["agent", "instruction"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_status",
            "description": "Consulta el estado, progreso, salida y entregables generados de una tarea persistente mediante su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID de la tarea persistente (ej: 'task_20261018_...')."
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_agent_tasks",
            "description": "Lista las tareas persistentes registradas en el motor de tareas con filtro de estado opcional.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "description": "Filtro opcional ('queued', 'waiting_approval', 'running', 'completed', 'failed', 'interrupted', 'cancelled')."
                    }
                },
                "required": []
            }
        }
    }
]

async def execute_tool(tool_name: str, arguments: Dict[str, Any], conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """Ejecutor con lista blanca estricta. Jamás ejecuta comandos de sistema arbitrarios."""
    log_event("tool_call_attempt", {"tool": tool_name, "args": arguments})

    try:
        if tool_name == "get_tv_status":
            res = await tv_service.get_status()
            return {"tool": tool_name, "result": res}

        elif tool_name == "turn_on_tv":
            res = await tv_service.set_power(power_on=True)
            return {"tool": tool_name, "result": res}

        elif tool_name == "turn_off_tv":
            res = await tv_service.set_power(power_on=False)
            return {"tool": tool_name, "result": res}

        elif tool_name == "launch_tv_app":
            app = arguments.get("app_name", "").strip()
            if not app:
                return {"tool": tool_name, "error": "Debe indicar el nombre de la aplicación."}
            res = await tv_service.launch_app(app)
            return {"tool": tool_name, "result": res}

        elif tool_name == "send_tv_key":
            key = arguments.get("key_name", "").strip().upper()
            if not key:
                return {"tool": tool_name, "error": "Debe especificar la tecla a enviar."}
            res = await tv_service.send_key(key)
            return {"tool": tool_name, "result": res}

        elif tool_name == "get_pc_vitals":
            res = await asyncio.to_thread(get_system_vitals)
            return {"tool": tool_name, "result": res}

        elif tool_name == "run_scene":
            scene = arguments.get("scene_name", "").strip().lower()
            from .scenes_service import scenes_service
            res = await scenes_service.execute_scene(scene)
            return {"tool": tool_name, "result": res}

        elif tool_name == "propose_memory_to_save":
            title = arguments.get("title", "").strip()
            category = arguments.get("category", "preferencias").strip().lower()
            access_level = arguments.get("access_level", "personal").strip().lower()
            content = arguments.get("content", "").strip()

            if not title or not content:
                return {"tool": tool_name, "error": "El título y contenido del recuerdo son obligatorios."}

            import secrets
            from .memory_db import memory_db, ALLOWED_CATEGORIES, ALLOWED_ACCESS_LEVELS

            if category not in ALLOWED_CATEGORIES:
                category = "general"
            if access_level not in ALLOWED_ACCESS_LEVELS:
                access_level = "personal"

            mem_id = f"mem_chat_{secrets.token_hex(4)}"
            created = memory_db.insert_memory(
                mem_id=mem_id,
                category=category,
                access_level=access_level,
                title=title,
                content=content,
                validity="permanent",
                origin="chat_ollama",
                review_required=True,
                status="PENDING_REVIEW"
            )
            log_event("memory_proposed_from_chat", {"id": mem_id, "title": title, "category": category})
            return {
                "tool": tool_name,
                "result": {
                    "id": mem_id,
                    "title": title,
                    "category": category,
                    "access_level": access_level,
                    "content": content,
                    "status": "PENDING_REVIEW",
                    "message": f"He propuesto guardar el recuerdo '{title}' en tu cola de revisión. Por favor confírmalo en la PWA para activarlo."
                }
            }

        elif tool_name == "get_agents_status":
            from .agent_inspector import agent_inspector
            res = agent_inspector.get_all_agents_overview()
            return {"tool": tool_name, "result": res}

        elif tool_name == "get_agent_tasks":
            from .agent_inspector import agent_inspector
            from .task_engine import task_engine
            from .approvals import approval_manager
            agent_filter = (arguments.get("agent_name") or "all").lower()
            status_filter = (arguments.get("status_filter") or "all").lower()
            codex_data = agent_inspector.inspect_codex_status(limit=6)
            gemini_data = agent_inspector.inspect_gemini_status(limit=3)
            hub_tasks = task_engine.list_tasks(limit=8)
            pending_approvals = approval_manager.get_pending()

            threads = codex_data.get("threads", [])
            if status_filter in ("interrupted", "pending"):
                threads = [t for t in threads if t.get("quota_exceeded") or t.get("status") == "INTERRUMPIDA_POR_CUOTA"]

            res = {
                "codex_tasks": threads,
                "codex_quota_exhausted": codex_data.get("is_quota_exhausted", False),
                "codex_quota_resets_at": codex_data.get("active_quota_resets_at"),
                "codex_quota_message": codex_data.get("active_quota_message"),
                "gemini_active_task": gemini_data.get("active_task"),
                "gemini_status": gemini_data.get("status"),
                "gemini_ready_to_continue": True,
                "hub_tasks": hub_tasks,
                "pending_approvals": pending_approvals
            }
            return {"tool": tool_name, "result": res}

        elif tool_name == "check_codex_quota":
            from .agent_inspector import agent_inspector
            codex_data = agent_inspector.inspect_codex_status(limit=5)
            interrupted = [t for t in codex_data.get("threads", []) if t.get("quota_exceeded")]
            res = {
                "is_quota_exhausted": codex_data.get("is_quota_exhausted", False),
                "resets_at": codex_data.get("active_quota_resets_at"),
                "error_message": codex_data.get("active_quota_message"),
                "interrupted_task": interrupted[0] if interrupted else None,
                "alternative_agent_available": "gemini_cli",
                "alternative_agent_ready": True
            }
            return {"tool": tool_name, "result": res}

        elif tool_name == "get_project_context":
            from .agent_inspector import agent_inspector
            kw = arguments.get("project_keyword")
            res = agent_inspector.inspect_project_context(kw)
            return {"tool": tool_name, "result": res}

        elif tool_name == "request_task_handover":
            from .agent_inspector import agent_inspector
            tquery = arguments.get("task_query", "incompleto")
            target = arguments.get("target_agent", "gemini_cli")
            instructions = arguments.get("instructions")
            res = agent_inspector.prepare_task_handover(
                task_query=tquery,
                target_agent=target,
                instructions=instructions,
                conversation_id=conversation_id,
                create_durable_task=True
            )
            return {"tool": tool_name, "result": res}

        elif tool_name == "execute_agent_task":
            from .task_engine import task_engine
            agent = arguments.get("agent", "gemini_cli")
            instruction = arguments.get("instruction", "").strip()
            project_path = arguments.get("project_path") or r"C:\Yedid-Smart-Home\sandbox"
            risk_level = arguments.get("risk_level", "LOW").upper()

            if not instruction:
                return {"tool": tool_name, "error": "La instrucción no puede estar vacía."}

            try:
                created_task = task_engine.create_task(
                    agent=agent,
                    instruction=instruction,
                    project_path=project_path,
                    conversation_id=conversation_id,
                    risk_level=risk_level
                )
                return {"tool": tool_name, "result": created_task}
            except ValueError as e:
                return {"tool": tool_name, "error": f"Rechazado por política de seguridad: {str(e)}"}

        elif tool_name == "get_task_status":
            from .task_engine import task_engine
            task_id = arguments.get("task_id", "").strip()
            if not task_id:
                return {"tool": tool_name, "error": "Debe especificar el task_id."}
            task = task_engine.get_task(task_id)
            if not task:
                return {"tool": tool_name, "error": f"Tarea '{task_id}' no encontrada."}
            return {"tool": tool_name, "result": task}

        elif tool_name == "list_agent_tasks":
            from .task_engine import task_engine
            status_filter = arguments.get("status_filter")
            tasks = task_engine.list_tasks(status_filter=status_filter, limit=10)
            return {"tool": tool_name, "result": {"tasks": tasks, "total": len(tasks)}}

        else:
            err = f"Herramienta '{tool_name}' no reconocida o no permitida en la lista blanca."
            log_event("tool_call_rejected", {"tool": tool_name, "reason": "not_in_allowlist"})
            return {"tool": tool_name, "error": err}

    except Exception as e:
        err = f"Error ejecutando '{tool_name}': {str(e)}"
        log_event("tool_call_exception", {"tool": tool_name, "error": str(e)})
        return {"tool": tool_name, "error": err}
