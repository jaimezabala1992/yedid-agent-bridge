"""
hub/task_engine.py

Motor duradero y persistente de tareas para Yedid Smart Hub v2.0:
- Almacenamiento transaccional en SQLite (data/agent_tasks.db) con modo WAL.
- Estados duraderos: queued, waiting_approval, running, completed, failed, interrupted, cancelled.
- Ejecución asíncrona independiente en segundo plano del servidor (independiente de si la PWA o el navegador se cierran).
- Reconciliación segura al reiniciar el servidor (marca 'running' abandonadas como 'interrupted' sin lanzar duplicados).
- Capacidad de reanudación segura de tareas interrumpidas (resume_task) con control idempotente de reintentos.
- Integración estricta con Human-in-the-Loop (ApprovalManager) vinculada a scope, ruta y hash de instrucción.
- Clasificación determinista de riesgo (inmune a prompt injection) y validación estricta de rutas de proyecto.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent_adapters import GeminiCLIAdapter, SafeScriptAdapter
from .security_policy import (
    validate_project_path,
    is_sensitive_path,
    classify_task_risk,
    sanitize_instruction,
    validate_instruction_security
)
from .approvals import approval_manager
from .chat_history_db import chat_history_db
from .process_guard import process_guard

logger = logging.getLogger("yedid_task_engine")

TASKS_DB_FILE = Path("C:/Yedid-Smart-Home/data/agent_tasks.db")


class TaskEngine:
    """Motor de gestión duradera y ejecución en segundo plano para tareas de agentes locales."""

    def __init__(self, db_path: Path = TASKS_DB_FILE):
        self.db_path = db_path
        self._worker_task: Optional[asyncio.Task] = None
        self._wake_event = asyncio.Event()
        self._running = False
        self._gemini_adapter = GeminiCLIAdapter()
        self._script_adapter = SafeScriptAdapter()
        self._active_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        """Crea el esquema relacional e índices si no existen."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    agent TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'waiting_approval', 'running', 'completed', 'failed', 'interrupted', 'cancelled')),
                    risk_level TEXT NOT NULL CHECK(risk_level IN ('LOW', 'HIGH')),
                    approval_id TEXT,
                    created_at REAL NOT NULL,
                    created_iso TEXT NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    duration_seconds REAL,
                    exit_code INTEGER,
                    stdout TEXT DEFAULT '',
                    stderr TEXT DEFAULT '',
                    deliverables TEXT DEFAULT '[]',
                    error TEXT,
                    metadata TEXT DEFAULT '{}'
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_conv ON tasks(conversation_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_approval ON tasks(approval_id);")
            conn.commit()

    def reconcile_on_startup(self) -> Dict[str, Any]:
        """
        Reconciliación de arranque:
        - Tareas que estaban 'running' al apagarse el servidor se marcan como 'interrupted'.
        - Limpia cualquier proceso huérfano de agy.exe o adaptadores en el sistema operativo.
        - No se relanzan automáticamente para evitar duplicaciones no deseadas.
        - Se ejecuta un checkpoint del WAL para asegurar integridad en disco.
        """
        now = time.time()
        interrupted_count = 0
        queued_count = 0

        # Limpiar procesos huérfanos que pudieran haber quedado si el Hub se cayó o Windows se reinició
        orphans_killed = process_guard.cleanup_orphan_agy_processes()
        if orphans_killed > 0:
            logger.info(f"[TASK ENGINE] Se terminaron {orphans_killed} procesos agy.exe huérfanos en arranque.")

        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'interrupted', finished_at = ?, error = 'Hub reiniciado mientras la tarea estaba en ejecución.' WHERE status = 'running';",
                (now,)
            )
            interrupted_count = cursor.rowcount

            cur_q = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'queued';")
            queued_count = cur_q.fetchone()["c"]
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass

        logger.info(f"[TASK ENGINE] Reconciliación completada: {interrupted_count} tareas interrumpidas reconciliadas, {queued_count} tareas en cola.")
        return {
            "interrupted_reconciled": interrupted_count,
            "queued_pending": queued_count,
            "orphans_cleaned": orphans_killed
        }

    def start_worker(self) -> None:
        """Inicia el worker asíncrono de ejecución en segundo plano."""
        if self._worker_task is None or self._worker_task.done():
            self._running = True
            self._wake_event = asyncio.Event()
            loop = asyncio.get_event_loop()
            self._worker_task = loop.create_task(self._worker_loop())
            logger.info("[TASK ENGINE] Worker de ejecución en segundo plano iniciado.")

    def stop_worker(self) -> None:
        """Detiene el worker de tareas de manera ordenada, termina Job Objects y sincroniza WAL."""
        self._running = False
        self._wake_event.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
        # Terminar todos los procesos/jobs activos
        try:
            process_guard.cleanup_all()
        except Exception as e:
            logger.warning(f"[TASK ENGINE] Error limpiando process_guard: {e}")
        try:
            with self._get_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        logger.info("[TASK ENGINE] Worker detenido.")

    def create_task(
        self,
        agent: str,
        instruction: str,
        project_path: str,
        conversation_id: Optional[str] = None,
        risk_level: str = "LOW",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Crea una nueva tarea duradera bajo estricta política de seguridad:
        - Valida que project_path pertenezca a las rutas autorizadas y no sea zona sensible.
        - Sanitiza la instrucción para prevenir inyección de caracteres o comandos.
        - Determina el nivel de riesgo de forma determinista (inmune a prompt injection).
        - Si el riesgo es 'HIGH', genera una solicitud HITL en ApprovalManager con huella criptográfica.
        """
        valid, resolved_path, err_reason = validate_project_path(project_path)
        if not valid or not resolved_path:
            raise ValueError(f"Directorio de proyecto no autorizado: {err_reason}")

        sanitized = sanitize_instruction(instruction)
        if not sanitized:
            raise ValueError("La instrucción de la tarea no puede estar vacía.")

        valid_instr, err_instr = validate_instruction_security(sanitized)
        if not valid_instr:
            raise ValueError(err_instr)

        # Clasificación determinista de riesgo (no confiamos en el nivel propuesto por el LLM si es riesgoso)
        computed_risk = classify_task_risk(sanitized, resolved_path)
        effective_risk = "HIGH" if (risk_level.upper() == "HIGH" or computed_risk == "HIGH") else "LOW"

        task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = time.time()
        created_iso = datetime.now(timezone.utc).isoformat()
        metadata_dict = metadata or {}
        metadata_dict["retry_count"] = 0

        status = "queued"
        approval_id = None

        if effective_risk == "HIGH":
            status = "waiting_approval"
            instr_hash = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
            approval_rec = approval_manager.create_request(
                action="execute_agent_task",
                description=f"Ejecución de tarea con agente '{agent}' en {resolved_path}: '{sanitized[:80]}...'",
                params={
                    "task_id": task_id,
                    "agent": agent,
                    "instruction": sanitized,
                    "project_path": str(resolved_path),
                    "instruction_hash": instr_hash,
                    "allowed_project_path": str(resolved_path)
                },
                risk_level="HIGH",
                requester="task_engine"
            )
            approval_id = approval_rec["id"]

        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO tasks (
                    id, conversation_id, agent, instruction, project_path,
                    status, risk_level, approval_id, created_at, created_iso, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                task_id, conversation_id, agent, sanitized, str(resolved_path),
                status, effective_risk, approval_id, now, created_iso, json.dumps(metadata_dict)
            ))
            conn.commit()

        logger.info(f"[TASK ENGINE] Tarea creada: {task_id} (Agente: {agent}, Estado: {status}, Riesgo: {effective_risk}, Aprobación: {approval_id})")

        if status == "queued":
            self._wake_event.set()

        return self.get_task(task_id)

    def on_approval_granted(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Transiciona una tarea de 'waiting_approval' a 'queued' cuando Jaime la autoriza."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE approval_id = ?;", (approval_id,)
            ).fetchone()
            if not row:
                return None
            task_id = row["id"]
            if row["status"] == "waiting_approval":
                conn.execute("UPDATE tasks SET status = 'queued' WHERE id = ?;", (task_id,))
                conn.commit()
                logger.info(f"[TASK ENGINE] Aprobación {approval_id} otorgada. Tarea {task_id} transicionada a 'queued'.")
                self._wake_event.set()
                return self.get_task(task_id)
        return None

    def on_approval_rejected(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Cancela una tarea cuando Jaime rechaza su solicitud de aprobación."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE approval_id = ?;", (approval_id,)
            ).fetchone()
            if not row:
                return None
            task_id = row["id"]
            if row["status"] == "waiting_approval":
                now = time.time()
                conn.execute(
                    "UPDATE tasks SET status = 'cancelled', finished_at = ?, error = 'Aprobación humana rechazada por el usuario.' WHERE id = ?;",
                    (now, task_id)
                )
                conn.commit()
                logger.info(f"[TASK ENGINE] Aprobación {approval_id} rechazada. Tarea {task_id} cancelada.")
                return self.get_task(task_id)
        return None

    def resume_task(self, task_id: str) -> Dict[str, Any]:
        """
        Recupera y reanuda de manera inteligente una tarea interrumpida o fallida:
        - Valida que la tarea exista y esté en 'interrupted' o 'failed'.
        - Escanea el espacio de trabajo para detectar entregables o archivos ya producidos.
        - Enriquese la instrucción (CONTINUACIÓN INTELIGENTE) para ordenar explícitamente al agente
          que no repita ni sobrescriba lo ya completado y continúe desde el último estado en disco.
        - Incrementa el contador de reintentos (retry_count).
        - Si era de alto riesgo y su token de aprobación expiró, genera una nueva solicitud HITL.
        - Despierta al worker si pasa a 'queued'.
        """
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Tarea '{task_id}' no encontrada.")
            task_dict = self._row_to_dict(row)
            cur_status = task_dict["status"]
            if cur_status not in ("interrupted", "failed"):
                raise ValueError(f"La tarea {task_id} no está en estado recuperable (estado actual: '{cur_status}').")

            meta = task_dict.get("metadata", {})
            retries = meta.get("retry_count", 0) + 1
            meta["retry_count"] = retries
            meta["resumed_at"] = time.time()

            # Escanear artefactos previos para continuidad inteligente
            project_path_obj = Path(task_dict["project_path"])
            existing_artifacts = []
            if project_path_obj.exists() and project_path_obj.is_dir():
                for p in project_path_obj.glob("**/*"):
                    if p.is_file() and not any(part.startswith(".") for part in p.parts):
                        try:
                            rel = str(p.relative_to(project_path_obj))
                            size = p.stat().st_size
                            existing_artifacts.append({"rel": rel, "size": size})
                        except Exception:
                            continue

            orig_instruction = meta.get("original_instruction") or task_dict["instruction"]
            meta["original_instruction"] = orig_instruction

            effective_instruction = orig_instruction
            if existing_artifacts:
                art_summary = "\n".join([f"- {a['rel']} ({a['size']} bytes)" for a in existing_artifacts[:10]])
                meta["resumed_from_files"] = [a["rel"] for a in existing_artifacts[:10]]
                effective_instruction = (
                    f"[CONTINUACIÓN INTELIGENTE - TAREA REANUDADA TRAS INTERRUPCIÓN]\n"
                    f"Artefactos y archivos previos detectados en el espacio de trabajo:\n"
                    f"{art_summary}\n\n"
                    f"REGLAS OBLIGATORIAS DE CONTINUIDAD:\n"
                    f"1. NO reinicies desde cero ni sobrescribas el trabajo ya completado o archivos válidos.\n"
                    f"2. Inspecciona el estado de los archivos arriba indicados y continúa la tarea desde el último punto verificado.\n"
                    f"3. Completa el objetivo final de la instrucción original sin duplicar acciones ya realizadas:\n"
                    f"--- OBJETIVO ORIGINAL ---\n"
                    f"{orig_instruction}\n"
                    f"--- FIN OBJETIVO ---"
                )

            new_status = "queued"
            approval_id = task_dict.get("approval_id")

            if task_dict.get("risk_level") == "HIGH":
                appr_rec = approval_manager.get_request(approval_id) if approval_id else None
                # Si la aprobación no existe, expiró o no está aprobada, requerir nueva aprobación
                if not appr_rec or appr_rec.get("status") != "APPROVED":
                    new_status = "waiting_approval"
                    resolved_path = task_dict["project_path"]
                    agent = task_dict["agent"]
                    instr_hash = hashlib.sha256(effective_instruction.encode("utf-8")).hexdigest()
                    new_appr = approval_manager.create_request(
                        action="execute_agent_task",
                        description=f"Reanudación de tarea '{agent}' en {resolved_path}: '{orig_instruction[:80]}...'",
                        params={
                            "task_id": task_id,
                            "agent": agent,
                            "instruction": effective_instruction,
                            "project_path": str(resolved_path),
                            "instruction_hash": instr_hash,
                            "allowed_project_path": str(resolved_path)
                        },
                        risk_level="HIGH",
                        requester="task_engine_resume"
                    )
                    approval_id = new_appr["id"]

            conn.execute("""
                UPDATE tasks SET
                    instruction = ?,
                    status = ?,
                    approval_id = ?,
                    error = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    metadata = ?
                WHERE id = ?;
            """, (effective_instruction, new_status, approval_id, json.dumps(meta), task_id))
            conn.commit()

        logger.info(f"[TASK ENGINE] Tarea {task_id} reanudada con éxito (estado: '{new_status}', intento #{retries}, archivos previos: {len(existing_artifacts)}).")
        if new_status == "queued":
            self._wake_event.set()

        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> bool:
        """Cancela una tarea en curso, en cola o esperando aprobación garantizando terminación en el kernel."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            if not row:
                return False
            cur_status = row["status"]
            if cur_status in ("completed", "failed", "cancelled", "interrupted"):
                return False

            now = time.time()
            conn.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = ?, error = 'Cancelada por el usuario.' WHERE id = ?;",
                (now, task_id)
            )
            conn.commit()

        # Terminar mediante Job Object en kernel de Windows (cierra todos los procesos hijos sin huérfanos)
        try:
            process_guard.terminate_task(task_id)
        except Exception as e:
            logger.warning(f"[TASK ENGINE] Error terminando job en cancel_task: {e}")

        proc = self._active_processes.get(task_id)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass

        logger.info(f"[TASK ENGINE] Tarea {task_id} cancelada manualmente y procesos asociados terminados.")
        return True

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene el registro completo de una tarea."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def list_tasks(
        self,
        status_filter: Optional[str] = None,
        conversation_id: Optional[str] = None,
        agent: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Lista tareas con filtros opcionales."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        if conversation_id:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        if agent:
            query += " AND agent = ?"
            params.append(agent)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["deliverables"] = json.loads(d.get("deliverables") or "[]")
        except Exception:
            d["deliverables"] = []
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except Exception:
            d["metadata"] = {}
        return d

    async def _worker_loop(self) -> None:
        """Loop asíncrono de ejecución de tareas en cola."""
        logger.info("[TASK ENGINE] Worker loop activo y escuchando eventos.")
        while self._running:
            try:
                task_row = self._fetch_next_queued_task()
                if not task_row:
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=3.0)
                        self._wake_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

                await self._execute_task_pipeline(task_row)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TASK ENGINE] Error inesperado en loop de worker: {e}", exc_info=True)
                await asyncio.sleep(2.0)

    def _fetch_next_queued_task(self) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1;"
            ).fetchone()
            if row:
                return self._row_to_dict(row)
        return None

    def _record_task_failure(self, task_id: str, error_message: str) -> None:
        now = time.time()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE tasks SET
                    status = 'failed',
                    finished_at = ?,
                    error = ?
                WHERE id = ? AND status != 'cancelled';
            """, (now, error_message, task_id))
            conn.commit()

    def _is_task_cancelled(self, task_id: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            return bool(row and row["status"] == "cancelled")

    async def _execute_task_pipeline(self, task: Dict[str, Any]) -> None:
        """Ejecuta una tarea de manera supervisada, validando tokens de aprobación de alcance único y evitando condiciones de carrera con cancel_task."""
        task_id = task["id"]
        agent = task["agent"]
        instruction = task["instruction"]
        project_path = Path(task["project_path"])
        conv_id = task.get("conversation_id")
        risk_level = task.get("risk_level", "LOW")

        logger.info(f"[TASK ENGINE] Iniciando ejecución de tarea {task_id} (Agente: {agent}, Riesgo: {risk_level})...")
        t_start = time.time()

        if self._is_task_cancelled(task_id):
            logger.info(f"[TASK ENGINE] Tarea {task_id} ya se encuentra cancelada antes de iniciar pipeline. Abortando.")
            return

        # Si es de alto riesgo, verificar exhaustivamente el token de aprobación humana
        if risk_level == "HIGH":
            approval_id = task.get("approval_id")
            if not approval_id:
                err = "Violación de seguridad: Tarea de alto riesgo sin identificador de aprobación."
                logger.error(f"[TASK ENGINE] {err}")
                self._record_task_failure(task_id, err)
                return

            expected_params = {
                "task_id": task_id,
                "agent": agent,
                "instruction": instruction,
                "project_path": str(project_path.resolve()),
                "instruction_hash": hashlib.sha256(instruction.strip().encode("utf-8")).hexdigest(),
                "allowed_project_path": str(project_path.resolve())
            }

            if not approval_manager.is_action_authorized(
                approval_id=approval_id,
                expected_action="execute_agent_task",
                expected_params=expected_params
            ):
                err = "Violación de seguridad: Aprobación no autorizada, expirada o discrepancia estricta de alcance/parámetros."
                logger.error(f"[TASK ENGINE] {err} (task: {task_id}, approval: {approval_id})")
                self._record_task_failure(task_id, err)
                return

        # Transición atómica (Compare-And-Set): solo pasa a 'running' si sigue en 'queued'
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued';",
                (t_start, task_id)
            )
            conn.commit()
            if cur.rowcount == 0:
                logger.info(f"[TASK ENGINE] Tarea {task_id} ya no está en estado 'queued' (posiblemente cancelada). Abortando ejecución.")
                return

        def on_proc_started(pid: int):
            process_guard.attach_process(task_id, pid)
            # Si fue cancelada justo en la ventana de arranque del subproceso, terminar de inmediato
            if self._is_task_cancelled(task_id):
                try:
                    process_guard.terminate_task(task_id)
                except Exception:
                    pass

        exec_res: Dict[str, Any] = {}
        try:
            if agent in ("gemini_cli", "gemini", "antigravity"):
                exec_res = await self._gemini_adapter.execute_task(
                    instruction=instruction,
                    project_path=project_path,
                    timeout_seconds=120.0,
                    allow_edits=True,
                    on_process_started=on_proc_started
                )
            elif agent in ("safe_script", "script", "test_worker"):
                exec_res = await self._script_adapter.execute_task(
                    instruction=instruction,
                    project_path=project_path,
                    timeout_seconds=60.0,
                    on_process_started=on_proc_started
                )
            else:
                exec_res = {
                    "success": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"Agente '{agent}' no reconocido para ejecución directa.",
                    "deliverables": []
                }

        except Exception as e:
            exec_res = {
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"Excepción durante la ejecución: {str(e)}",
                "deliverables": []
            }
        finally:
            process_guard.detach_task(task_id)

        t_finish = time.time()
        duration = round(t_finish - t_start, 2)
        success = exec_res.get("success", False)
        candidate_status = "completed" if success else "failed"
        stdout_txt = exec_res.get("stdout", "")
        stderr_txt = exec_res.get("stderr", "")
        exit_code = exec_res.get("returncode", 0 if success else -1)
        deliverables = exec_res.get("deliverables", [])
        error_msg = exec_res.get("error") or (stderr_txt if not success else None)

        with self._get_conn() as conn:
            # Actualizar únicamente si NO fue cancelada durante la ejecución
            cur = conn.execute("""
                UPDATE tasks SET
                    status = ?,
                    finished_at = ?,
                    duration_seconds = ?,
                    exit_code = ?,
                    stdout = ?,
                    stderr = ?,
                    deliverables = ?,
                    error = ?
                WHERE id = ? AND status != 'cancelled';
            """, (
                candidate_status, t_finish, duration, exit_code,
                stdout_txt, stderr_txt, json.dumps(deliverables, ensure_ascii=False),
                error_msg, task_id
            ))
            if cur.rowcount == 0:
                # La tarea fue cancelada mientras corría: preservar status='cancelled' y registrar telemetría final
                new_status = "cancelled"
                conn.execute("""
                    UPDATE tasks SET
                        finished_at = COALESCE(finished_at, ?),
                        duration_seconds = ?,
                        exit_code = ?,
                        stdout = ?,
                        stderr = ?,
                        deliverables = ?
                    WHERE id = ? AND status = 'cancelled';
                """, (
                    t_finish, duration, exit_code,
                    stdout_txt, stderr_txt, json.dumps(deliverables, ensure_ascii=False),
                    task_id
                ))
            else:
                new_status = candidate_status
            conn.commit()

        logger.info(f"[TASK ENGINE] Tarea {task_id} finalizada con estado '{new_status}' en {duration}s. Entregables: {len(deliverables)}.")

        if conv_id and new_status != "cancelled":
            try:
                deliverables_summary = ""
                if deliverables:
                    deliverables_summary = "\nArchivos producidos/modificados:\n" + "\n".join([f"- {d['name']} ({d['size_bytes']} bytes)" for d in deliverables[:5]])

                chat_msg = (
                    f"⚙️ **[Resultado de Tarea {task_id}]**\n"
                    f"Agente: `{agent}` | Estado: **{new_status.upper()}** ({duration}s)\n\n"
                    f"{stdout_txt[:400] if stdout_txt else (stderr_txt[:400] if stderr_txt else 'Ejecución completada.')}"
                    f"{deliverables_summary}"
                )
                chat_history_db.save_message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=chat_msg,
                    task_id=task_id
                )
            except Exception as e:
                logger.error(f"[TASK ENGINE] Error guardando resultado en chat_history: {e}")


task_engine = TaskEngine()
