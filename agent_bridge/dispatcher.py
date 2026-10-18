# -*- coding: utf-8 -*-
"""
agent_bridge/dispatcher.py

Despachador central para Yedid Agent Bridge.
- Orquesta la consulta de GitHub Issues, validación, enrutamiento a adaptadores y publicación de resultados.
- Control de idempotencia, seguridad y sanitización de salidas.
"""

import time
import logging
from typing import Dict, Any, Optional, List
from .config import load_bridge_config, save_bridge_config, get_github_token, is_bridge_paused
from .schemas import extract_and_parse_task, AgentTask, AgentResult
from .security import (
    SecurityPolicyEngine,
    DECISION_AUTO_ALLOW,
    DECISION_REQUIRE_HITL,
    DECISION_REJECT
)
from .task_store import TaskStore
from .github_client import GitHubBridgeClient, GitHubSecurityException
from .adapters.base import BaseAgentAdapter
from .adapters.ollama_adapter import OllamaAdapter
from .adapters.gemini_adapter import GeminiAdapter
from .adapters.codex_adapter import CodexAdapter
from .adapters.hub_adapter import HubAdapter

logger = logging.getLogger("YedidAgentBridge.Dispatcher")

class BridgeDispatcher:
    """Coordina el flujo de trabajo bidireccional entre GitHub y los agentes locales."""

    def __init__(self):
        self.config = load_bridge_config()
        self.task_store = TaskStore()
        self.policy_engine = SecurityPolicyEngine(self.config.get("authorized_users"))

        # Registro de adaptadores disponibles
        self.adapters: Dict[str, BaseAgentAdapter] = {
            "ollama": OllamaAdapter(),
            "gemini": GeminiAdapter(),
            "codex": CodexAdapter(),
            "yedid_hub": HubAdapter()
        }

    def get_github_client(self) -> Optional[GitHubBridgeClient]:
        """Inicializa el cliente de GitHub con el token activo."""
        token = get_github_token()
        if not token:
            return None
        return GitHubBridgeClient(token=token)

    async def poll_and_process_cycle(self) -> Dict[str, Any]:
        """Ejecuta un ciclo completo de consulta y procesamiento de Issues."""
        # 1. Comprobar si el Bridge está pausado
        if is_bridge_paused():
            logger.debug("Bridge pausado por configuración del usuario.")
            return {"status": "paused", "processed_count": 0}

        # 2. Comprobar disponibilidad de token
        client = self.get_github_client()
        if not client:
            logger.debug("Token de GitHub no configurado. Esperando credenciales...")
            return {"status": "no_token", "processed_count": 0}

        # 3. Resolver propietario del repositorio si no está configurado
        owner = self.config.get("repo_owner")
        repo = self.config.get("repo_name", "yedid-agent-bridge")
        if not owner:
            try:
                user_info = await client.get_authenticated_user()
                owner = user_info.get("login", "")
                if owner:
                    self.config["repo_owner"] = owner
                    # Agregar al usuario autenticado como operador autorizado si no está
                    if owner.lower() not in self.policy_engine.authorized_users:
                        self.policy_engine.authorized_users.append(owner.lower())
                    save_bridge_config(self.config)
                    logger.info(f"Propietario detectado automáticamente: {owner}")
            except Exception as e:
                logger.error(f"Error detectando usuario de GitHub: {e}")
                return {"status": "auth_error", "error": str(e), "processed_count": 0}

        # 4. Verificar que el repositorio sea estrictamente privado
        try:
            await client.verify_repo_is_private(owner, repo)
        except GitHubSecurityException as e:
            logger.critical(f"Seguridad comprometida: {e}")
            return {"status": "security_violation", "error": str(e), "processed_count": 0}
        except FileNotFoundError:
            logger.info(f"El repositorio {owner}/{repo} aún no existe. Listo para ser creado.")
            return {"status": "repo_not_found", "repo": f"{owner}/{repo}", "processed_count": 0}
        except Exception as e:
            logger.error(f"Error comprobando repositorio {owner}/{repo}: {e}")
            return {"status": "connection_error", "error": str(e), "processed_count": 0}

        # 5. Obtener Issues abiertos
        try:
            issues = await client.fetch_open_issues(owner, repo, limit=self.config.get("max_issues_per_poll", 10))
        except Exception as e:
            logger.error(f"Error obteniendo issues de {owner}/{repo}: {e}")
            return {"status": "fetch_error", "error": str(e), "processed_count": 0}

        processed_count = 0
        skipped_count = 0

        for issue in issues:
            issue_number = issue.get("number")
            issue_url = issue.get("html_url", "")
            author = issue.get("user", {}).get("login", "")
            body = issue.get("body", "")

            # Extraer tarea con esquema oficial
            task = extract_and_parse_task(body)
            if not task:
                # El issue no contiene un formato yedid-agent-task/1.0 válido, omitir
                skipped_count += 1
                continue

            # Idempotencia: Verificar si ya fue procesada
            if self.task_store.is_task_processed(task.task_id):
                skipped_count += 1
                continue

            logger.info(f"Nueva tarea detectada: {task.task_id} (Agente: {task.target_agent}, Op: {task.operation}) en Issue #{issue_number}")

            # Registrar inicio en almacenamiento local
            self.task_store.record_task_start(
                task_id=task.task_id,
                issue_number=issue_number,
                issue_url=issue_url,
                author=author,
                target_agent=task.target_agent,
                operation=task.operation,
                request_data=task.model_dump()
            )

            # Evaluar política de seguridad
            decision, reason = self.policy_engine.evaluate(task, author)

            if decision == DECISION_REJECT:
                logger.warning(f"Tarea {task.task_id} RECHAZADA: {reason}")
                res = AgentResult(
                    task_id=task.task_id,
                    agent=task.target_agent,
                    status="failed",
                    result=f"SOLICITUD RECHAZADA POR POLÍTICA DE SEGURIDAD: {reason}",
                    metadata={"security_decision": DECISION_REJECT}
                )
                await client.post_issue_comment(owner, repo, issue_number, res.to_github_comment())
                await client.add_issue_labels(owner, repo, issue_number, ["yedid-rejected"])
                self.task_store.update_task_finish(task.task_id, status="failed", result_data=res.model_dump(), error_message=reason)
                processed_count += 1
                continue

            elif decision == DECISION_REQUIRE_HITL:
                logger.info(f"Tarea {task.task_id} REQUIERE HITL: {reason}")
                res = AgentResult(
                    task_id=task.task_id,
                    agent=task.target_agent,
                    status="waiting_approval",
                    result=f"ACCIÓN REQUIERE APROBACIÓN HUMANA (HITL): {reason}. Confirma en la PWA de Yedid.",
                    requires_followup=True,
                    metadata={"security_decision": DECISION_REQUIRE_HITL}
                )
                await client.post_issue_comment(owner, repo, issue_number, res.to_github_comment())
                await client.add_issue_labels(owner, repo, issue_number, ["yedid-pending-approval"])
                self.task_store.update_task_finish(task.task_id, status="waiting_approval", result_data=res.model_dump())
                processed_count += 1
                continue

            # DECISION_AUTO_ALLOW: Despachar a adaptador correspondiente
            adapter = self.adapters.get(task.target_agent.lower())
            if not adapter:
                err_msg = f"No existe un adaptador registrado para el agente '{task.target_agent}'"
                res = AgentResult(task_id=task.task_id, agent=task.target_agent, status="failed", result=err_msg)
                await client.post_issue_comment(owner, repo, issue_number, res.to_github_comment())
                self.task_store.update_task_finish(task.task_id, status="failed", error_message=err_msg)
                processed_count += 1
                continue

            try:
                # Ejecutar tarea en adaptador
                result = await adapter.execute(task)

                # Sanitizar cualquier dato confidencial o token antes de salir a GitHub
                result.result = self.policy_engine.sanitize_output(result.result)

                # Publicar comentario en GitHub
                await client.post_issue_comment(owner, repo, issue_number, result.to_github_comment())

                # Etiquetar Issue
                processed_label = self.config.get("auto_label_processed", "yedid-processed")
                await client.add_issue_labels(owner, repo, issue_number, [processed_label])

                # Opcional: auto-cerrar issue si está configurado
                if self.config.get("auto_close_completed", False) and result.status == "completed":
                    await client.close_issue(owner, repo, issue_number)

                # Persistir finalización en BD local
                exec_time = result.metadata.get("execution_time_sec", 0.0) if result.metadata else 0.0
                self.task_store.update_task_finish(
                    task.task_id,
                    status=result.status,
                    result_data=result.model_dump(),
                    execution_time_sec=exec_time
                )
                logger.info(f"Tarea {task.task_id} procesada exitosamente con estado '{result.status}'")
                processed_count += 1

            except Exception as e:
                logger.error(f"Error ejecutando tarea {task.task_id}: {e}")
                fail_res = AgentResult(
                    task_id=task.task_id,
                    agent=task.target_agent,
                    status="failed",
                    result=f"Error interno procesando tarea: {str(e)}"
                )
                await client.post_issue_comment(owner, repo, issue_number, fail_res.to_github_comment())
                self.task_store.update_task_finish(task.task_id, status="failed", error_message=str(e))
                processed_count += 1

        return {
            "status": "ok",
            "processed_count": processed_count,
            "skipped_count": skipped_count
        }
