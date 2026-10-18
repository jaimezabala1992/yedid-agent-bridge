# -*- coding: utf-8 -*-
"""
agent_bridge/task_store.py

Almacén SQLite local y persistente para Yedid Agent Bridge.
- Garantiza idempotencia: una misma tarea jamás se ejecuta dos veces.
- Registra auditoría local de tareas, tiempos y estados.
- Recuperación automática ante reinicios de Windows.
"""

import sqlite3
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from .config import DB_PATH, ensure_data_dir

logger = logging.getLogger("YedidAgentBridge.TaskStore")

class TaskStore:
    """Gestiona la persistencia de tareas y resultados en SQLite local."""

    def __init__(self, db_path: Optional[Path] = None):
        ensure_data_dir()
        self.db_path = db_path or DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Crea las tablas necesarias si no existen."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bridge_tasks (
                    task_id TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    issue_url TEXT,
                    author TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT,
                    result_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    execution_time_sec REAL DEFAULT 0.0
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bridge_tasks_issue 
                ON bridge_tasks(issue_number);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bridge_tasks_status 
                ON bridge_tasks(status);
            """)
            conn.commit()

    def is_task_processed(self, task_id: str) -> bool:
        """Comprueba si un task_id ya ha sido procesado o está en curso."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT status FROM bridge_tasks WHERE task_id = ?",
                (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False
            # Si ya está completado, fallido o cancelado, se considera finalizado
            return row["status"] in ("completed", "failed", "cancelled", "running")

    def record_task_start(
        self,
        task_id: str,
        issue_number: int,
        issue_url: str,
        author: str,
        target_agent: str,
        operation: str,
        request_data: Dict[str, Any]
    ) -> bool:
        """Registra el inicio del procesamiento de una tarea."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO bridge_tasks (
                        task_id, issue_number, issue_url, author, target_agent,
                        operation, status, request_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status = 'running',
                        updated_at = excluded.updated_at
                """, (
                    task_id, issue_number, issue_url, author, target_agent,
                    operation, json.dumps(request_data, ensure_ascii=False),
                    now, now
                ))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error registrando inicio de tarea {task_id}: {e}")
            return False

    def update_task_finish(
        self,
        task_id: str,
        status: str,
        result_data: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        execution_time_sec: float = 0.0
    ) -> bool:
        """Actualiza el estado final de una tarea (completed, failed, waiting_approval, etc.)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        res_str = json.dumps(result_data, ensure_ascii=False) if result_data else None
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE bridge_tasks SET
                        status = ?,
                        result_json = ?,
                        error_message = ?,
                        updated_at = ?,
                        execution_time_sec = ?
                    WHERE task_id = ?
                """, (status, res_str, error_message, now, execution_time_sec, task_id))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error actualizando tarea {task_id}: {e}")
            return False

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene una tarea por su ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM bridge_tasks WHERE task_id = ?",
                (task_id,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def list_recent_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Lista las tareas más recientes registradas."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM bridge_tasks ORDER BY rowid DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas cuantitativas del Bridge."""
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM bridge_tasks").fetchone()[0]
            completed = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status = 'completed'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status = 'failed'").fetchone()[0]
            waiting = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status = 'waiting_approval'").fetchone()[0]
            return {
                "total_tasks": total,
                "completed": completed,
                "failed": failed,
                "waiting_approval": waiting
            }
