"""
hub/chat_history_db.py

Base de datos SQLite persistente y transaccional para el historial de conversaciones de Yedid.
- Fuente de verdad en el servidor (sobrevive a recargas, cambios de pestañas y reinicios del Hub).
- Identificadores de conversación estables (UUID) por usuario.
- Idempotencia transaccional completa por client_message_id (estados: processing, completed, failed).
- Vinculación explícita entre cada respuesta del asistente y su mensaje de solicitud original (reply_to_message_id / request_client_message_id).
- Vinculación directa con IDs de tareas del TaskEngine.
- Separado estrictamente del Memory Engine v2.2 (no contamina la memoria a largo plazo).
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import get_data_dir, assert_not_production_db_in_test

logger = logging.getLogger("yedid_chat_db")

DATA_DIR = get_data_dir()
DB_PATH = DATA_DIR / "chat_history.db"


class ChatHistoryDB:
    def __init__(self, db_path: Optional[Path] = None):
        self._custom_db_path = db_path
        self._initialized_path: Optional[Path] = None
        if db_path is not None:
            self._init_db()

    @property
    def db_path(self) -> Path:
        if self._custom_db_path is not None:
            return self._custom_db_path
        env_db = os.getenv("YEDID_CHAT_DB_PATH")
        if env_db:
            return Path(env_db).resolve()
        return get_data_dir() / "chat_history.db"

    @db_path.setter
    def db_path(self, value: Path) -> None:
        self._custom_db_path = value
        self._initialized_path = None

    def _open_raw_connection(self, isolation_level: Optional[str] = "") -> sqlite3.Connection:
        current = self.db_path
        assert_not_production_db_in_test(current)
        current.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(current), timeout=15.0, isolation_level=isolation_level)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    def _get_connection(self, isolation_level: Optional[str] = "") -> sqlite3.Connection:
        current = self.db_path
        if self._initialized_path != current:
            self._init_db()
        return self._open_raw_connection(isolation_level=isolation_level)

    def _init_db(self) -> None:
        """Inicializa las tablas de conversaciones, mensajes y estado transaccional de solicitudes."""
        current = self.db_path
        with self._open_raw_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    is_archived INTEGER DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    tool_results TEXT,
                    task_id TEXT,
                    client_message_id TEXT UNIQUE,
                    reply_to_message_id TEXT,
                    request_client_message_id TEXT,
                    created_at REAL NOT NULL,
                    created_iso TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
                );
            """)

            # Migración segura de columnas explícitas en bases de datos preexistentes
            existing_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(messages);").fetchall()
            }
            if "reply_to_message_id" not in existing_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT;")
            if "request_client_message_id" not in existing_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN request_client_message_id TEXT;")

            # Tabla transaccional de idempotencia de solicitudes (processing | completed | failed)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_requests (
                    client_message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    user_message_id TEXT NOT NULL,
                    response_message_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
                    error_detail TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    FOREIGN KEY (user_message_id) REFERENCES messages(message_id) ON DELETE CASCADE,
                    FOREIGN KEY (response_message_id) REFERENCES messages(message_id) ON DELETE SET NULL
                );
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation_id, created_at ASC);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_client_id
                ON messages(client_message_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_reply_to
                ON messages(reply_to_message_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_req_client_id
                ON messages(request_client_message_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_requests_conv_status
                ON chat_requests(conversation_id, status);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user, updated_at DESC);
            """)
            conn.commit()
        self._initialized_path = current

    def checkpoint(self) -> None:
        """Fuerza un checkpoint del WAL a disco para asegurar persistencia permanente ante reinicios."""
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception as e:
            logger.warning(f"Error en checkpoint de chat_history: {e}")

    def get_or_create_conversation(
        self,
        conversation_id: Optional[str] = None,
        user: str = "jaime",
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Obtiene una conversación existente o crea una nueva con ID estable y conteo exacto."""
        now = time.time()
        with self._get_connection() as conn:
            if conversation_id:
                cur = conn.execute(
                    """
                    SELECT c.*,
                           (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count
                    FROM conversations c
                    WHERE c.conversation_id = ?;
                    """,
                    (conversation_id,)
                )
                row = cur.fetchone()
                if row:
                    d = dict(row)
                    d["id"] = d["conversation_id"]
                    return d

            cid = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
            t = title or "Nueva Conversación"
            conn.execute("""
                INSERT OR IGNORE INTO conversations (conversation_id, user, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (cid, user, t, now, now))
            conn.commit()
            cur2 = conn.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count
                FROM conversations c
                WHERE c.conversation_id = ?;
                """,
                (cid,)
            )
            row2 = cur2.fetchone()
            if row2:
                d2 = dict(row2)
                d2["id"] = d2["conversation_id"]
                return d2
            return {
                "id": cid,
                "conversation_id": cid,
                "user": user,
                "title": t,
                "created_at": now,
                "updated_at": now,
                "is_archived": 0,
                "message_count": 0
            }

    def create_conversation(
        self,
        user_id: str = "jaime",
        title: Optional[str] = None,
        conversation_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Alias conveniente para crear una conversación."""
        return self.get_or_create_conversation(conversation_id=conversation_id, user=user_id, title=title)

    def get_or_create_active_conversation(self, user_id: str = "jaime") -> Dict[str, Any]:
        """Obtiene la conversación más reciente o crea una nueva."""
        convs = self.list_conversations(user=user_id, limit=1)
        if convs:
            return convs[0]
        return self.create_conversation(user_id=user_id, title="Nueva Conversación")

    def is_message_duplicate(self, client_message_id: Optional[str]) -> bool:
        """Verifica si un mensaje o solicitud con este client_message_id ya fue registrado."""
        if not client_message_id:
            return False
        with self._get_connection() as conn:
            req_row = conn.execute(
                "SELECT 1 FROM chat_requests WHERE client_message_id = ? LIMIT 1;",
                (client_message_id,)
            ).fetchone()
            if req_row is not None:
                return True
            msg_row = conn.execute(
                "SELECT 1 FROM messages WHERE client_message_id = ? LIMIT 1;",
                (client_message_id,)
            ).fetchone()
            return msg_row is not None

    @staticmethod
    def _owner_email() -> str:
        return os.getenv("YEDID_OWNER_EMAIL", "owner@example.com")

    def list_conversations(self, user: Optional[str] = None, user_id: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
        """Lista las conversaciones activas ordenadas por última actualización con conteo real en SQLite."""
        effective_user = user or user_id
        owner_email = self._owner_email()
        with self._get_connection() as conn:
            if effective_user:
                cur = conn.execute("""
                    SELECT c.*, 
                           (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count,
                           (SELECT m.content FROM messages m WHERE m.conversation_id = c.conversation_id ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_message
                    FROM conversations c
                    WHERE (c.user = ? OR c.user IN ('jaime', ?, 'api_client')) AND c.is_archived = 0
                    ORDER BY c.updated_at DESC
                    LIMIT ?
                """, (effective_user, owner_email, limit))
            else:
                cur = conn.execute("""
                    SELECT c.*, 
                           (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count,
                           (SELECT m.content FROM messages m WHERE m.conversation_id = c.conversation_id ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_message
                    FROM conversations c
                    WHERE c.is_archived = 0
                    ORDER BY c.updated_at DESC
                    LIMIT ?
                """, (limit,))
            res = []
            for r in cur.fetchall():
                d = dict(r)
                d["id"] = d["conversation_id"]
                d["message_count"] = int(d.get("message_count") or 0)
                res.append(d)
            return res

    def get_conversation(self, conversation_id: str, user: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Consulta metadata y conteo exacto de mensajes de una conversación en SQLite."""
        owner_email = self._owner_email()
        with self._get_connection() as conn:
            if user:
                cur = conn.execute(
                    """
                    SELECT c.*,
                           (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count
                    FROM conversations c
                    WHERE c.conversation_id = ? AND (c.user = ? OR c.user IN ('jaime', ?, 'api_client'))
                    """,
                    (conversation_id, user, owner_email)
                )
            else:
                cur = conn.execute(
                    """
                    SELECT c.*,
                           (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count
                    FROM conversations c
                    WHERE c.conversation_id = ?
                    """,
                    (conversation_id,)
                )
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["id"] = d["conversation_id"]
                d["message_count"] = int(d.get("message_count") or 0)
                return d
            return None

    def update_conversation_title(self, conversation_id: str, title: str) -> bool:
        """Actualiza el título descriptivo de una conversación."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE conversation_id = ?",
                (title.strip(), time.time(), conversation_id)
            )
            conn.commit()
            return cur.rowcount > 0

    def begin_idempotent_request(
        self,
        conversation_id: str,
        client_message_id: str,
        user_content: str,
        user: str = "jaime"
    ) -> Dict[str, Any]:
        """
        Reclama atómicamente el procesamiento de una solicitud identificada por `client_message_id`
        mediante una transacción `BEGIN IMMEDIATE` en SQLite.
        - Si no existe: inserta el mensaje de usuario y crea el registro en `chat_requests` con estado `'processing'`,
          retornando `claimed=True`.
        - Si ya existe: retorna `claimed=False` junto con el estado actual (`'processing'`, `'completed'` o `'failed'`)
          y la respuesta vinculada si ya fue completada.
        """
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                INSERT OR IGNORE INTO conversations (conversation_id, user, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (conversation_id, user, "Nueva Conversación", now, now))

            req_row = conn.execute(
                "SELECT * FROM chat_requests WHERE client_message_id = ? LIMIT 1;",
                (client_message_id,)
            ).fetchone()

            if req_row is not None:
                status = req_row["status"]
                user_msg_id = req_row["user_message_id"]
                resp_msg_id = req_row["response_message_id"]
                req_conv_id = req_row["conversation_id"]
                err_detail = req_row["error_detail"]
                assistant_msg = None
                if resp_msg_id:
                    asst_row = conn.execute(
                        "SELECT * FROM messages WHERE message_id = ? LIMIT 1;",
                        (resp_msg_id,)
                    ).fetchone()
                    if asst_row:
                        assistant_msg = self._parse_message_row(asst_row)
                if assistant_msg is None:
                    asst_row = conn.execute(
                        """
                        SELECT * FROM messages
                        WHERE sender = 'assistant'
                          AND (request_client_message_id = ? OR reply_to_message_id = ?)
                        ORDER BY created_at ASC, rowid ASC
                        LIMIT 1;
                        """,
                        (client_message_id, user_msg_id)
                    ).fetchone()
                    if asst_row:
                        assistant_msg = self._parse_message_row(asst_row)
                conn.execute("COMMIT;")
                return {
                    "claimed": False,
                    "status": status,
                    "client_message_id": client_message_id,
                    "conversation_id": req_conv_id,
                    "user_message_id": user_msg_id,
                    "response_message_id": assistant_msg["message_id"] if assistant_msg else resp_msg_id,
                    "assistant_message": assistant_msg,
                    "error_detail": err_detail,
                }

            # Compatibilidad con mensajes previos a la tabla chat_requests
            existing_user_msg = conn.execute(
                "SELECT * FROM messages WHERE client_message_id = ? LIMIT 1;",
                (client_message_id,)
            ).fetchone()
            if existing_user_msg is not None:
                u_msg_id = existing_user_msg["message_id"]
                u_conv_id = existing_user_msg["conversation_id"]
                conn.execute("COMMIT;")
                legacy_resp = self.get_response_for_client_message_id(
                    client_message_id=client_message_id,
                    conversation_id=u_conv_id
                )
                inferred_status = "completed" if legacy_resp else "processing"
                resp_id = legacy_resp["message_id"] if legacy_resp else None
                with self._get_connection() as c2:
                    c2.execute("""
                        INSERT OR IGNORE INTO chat_requests (
                            client_message_id, conversation_id, user_message_id,
                            response_message_id, status, error_detail, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """, (client_message_id, u_conv_id, u_msg_id, resp_id, inferred_status, now, now))
                    c2.commit()
                return {
                    "claimed": False,
                    "status": inferred_status,
                    "client_message_id": client_message_id,
                    "conversation_id": u_conv_id,
                    "user_message_id": u_msg_id,
                    "response_message_id": resp_id,
                    "assistant_message": legacy_resp,
                    "error_detail": None,
                }

            # Solicitud nueva: insertar mensaje de usuario y registrar solicitud en estado 'processing'
            user_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO messages (
                    message_id, conversation_id, sender, content,
                    tool_calls, tool_results, task_id, client_message_id,
                    reply_to_message_id, request_client_message_id,
                    created_at, created_iso
                ) VALUES (?, ?, 'user', ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?)
            """, (
                user_msg_id, conversation_id, user_content,
                client_message_id, client_message_id, now, now_iso
            ))
            conn.execute("""
                INSERT INTO chat_requests (
                    client_message_id, conversation_id, user_message_id,
                    response_message_id, status, error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 'processing', NULL, ?, ?)
            """, (client_message_id, conversation_id, user_msg_id, now, now))
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id)
            )
            conn.execute("COMMIT;")
            return {
                "claimed": True,
                "status": "processing",
                "client_message_id": client_message_id,
                "conversation_id": conversation_id,
                "user_message_id": user_msg_id,
                "response_message_id": None,
                "assistant_message": None,
                "error_detail": None,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def complete_idempotent_request(
        self,
        conversation_id: str,
        user_message_id: Optional[str],
        content: str,
        tools_called: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        client_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Registra atómicamente la respuesta del asistente vinculándola explícitamente con
        `user_message_id` (`reply_to_message_id`) y `client_message_id` (`request_client_message_id`),
        y marca la solicitud en `chat_requests` como `'completed'`.
        """
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        tc_json = json.dumps(tools_called, ensure_ascii=False) if tools_called else None
        tr_json = json.dumps(tool_results, ensure_ascii=False) if tool_results else None

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            if client_message_id:
                req_row = conn.execute(
                    "SELECT * FROM chat_requests WHERE client_message_id = ? LIMIT 1;",
                    (client_message_id,)
                ).fetchone()
                if req_row is not None:
                    if not user_message_id:
                        user_message_id = req_row["user_message_id"]
                    if req_row["response_message_id"]:
                        existing_asst = conn.execute(
                            "SELECT * FROM messages WHERE message_id = ? LIMIT 1;",
                            (req_row["response_message_id"],)
                        ).fetchone()
                        if existing_asst is not None:
                            conn.execute("COMMIT;")
                            return self._parse_message_row(existing_asst)

            asst_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO messages (
                    message_id, conversation_id, sender, content,
                    tool_calls, tool_results, task_id, client_message_id,
                    reply_to_message_id, request_client_message_id,
                    created_at, created_iso
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """, (
                asst_msg_id, conversation_id, content,
                tc_json, tr_json, task_id,
                user_message_id, client_message_id,
                now, now_iso
            ))

            if client_message_id:
                conn.execute("""
                    UPDATE chat_requests
                    SET status = 'completed',
                        response_message_id = ?,
                        error_detail = NULL,
                        updated_at = ?
                    WHERE client_message_id = ?
                """, (asst_msg_id, now, client_message_id))

            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id)
            )
            conn.execute("COMMIT;")
            return {
                "message_id": asst_msg_id,
                "conversation_id": conversation_id,
                "sender": "assistant",
                "role": "assistant",
                "content": content,
                "tool_calls": tools_called or [],
                "tools_called": tools_called or [],
                "tool_results": tool_results,
                "task_id": task_id,
                "client_message_id": None,
                "reply_to_message_id": user_message_id,
                "request_client_message_id": client_message_id,
                "created_at": now,
                "created_iso": now_iso,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def fail_idempotent_request(
        self,
        client_message_id: Optional[str],
        error_detail: str,
        conversation_id: Optional[str] = None,
        user_message_id: Optional[str] = None,
        failure_reply: Optional[str] = None,
        tools_called: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Marca transaccionalmente una solicitud como `'failed'` en `chat_requests` y, opcionalmente,
        persiste el mensaje de error/fallback del asistente vinculado explícitamente a la solicitud.
        """
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        tc_json = json.dumps(tools_called, ensure_ascii=False) if tools_called else None

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            resp_msg_id: Optional[str] = None
            assistant_msg: Optional[Dict[str, Any]] = None

            if client_message_id:
                req_row = conn.execute(
                    "SELECT * FROM chat_requests WHERE client_message_id = ? LIMIT 1;",
                    (client_message_id,)
                ).fetchone()
                if req_row is not None:
                    conversation_id = conversation_id or req_row["conversation_id"]
                    user_message_id = user_message_id or req_row["user_message_id"]
                    resp_msg_id = req_row["response_message_id"]

            if failure_reply and conversation_id and not resp_msg_id:
                resp_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
                conn.execute("""
                    INSERT INTO messages (
                        message_id, conversation_id, sender, content,
                        tool_calls, tool_results, task_id, client_message_id,
                        reply_to_message_id, request_client_message_id,
                        created_at, created_iso
                    ) VALUES (?, ?, 'assistant', ?, ?, NULL, ?, NULL, ?, ?, ?, ?)
                """, (
                    resp_msg_id, conversation_id, failure_reply,
                    tc_json, task_id,
                    user_message_id, client_message_id,
                    now, now_iso
                ))
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                    (now, conversation_id)
                )
                assistant_msg = {
                    "message_id": resp_msg_id,
                    "conversation_id": conversation_id,
                    "sender": "assistant",
                    "role": "assistant",
                    "content": failure_reply,
                    "tool_calls": tools_called or [],
                    "tools_called": tools_called or [],
                    "tool_results": None,
                    "task_id": task_id,
                    "client_message_id": None,
                    "reply_to_message_id": user_message_id,
                    "request_client_message_id": client_message_id,
                    "created_at": now,
                    "created_iso": now_iso,
                }

            if client_message_id:
                conn.execute("""
                    UPDATE chat_requests
                    SET status = 'failed',
                        response_message_id = COALESCE(?, response_message_id),
                        error_detail = ?,
                        updated_at = ?
                    WHERE client_message_id = ?
                """, (resp_msg_id, error_detail, now, client_message_id))

            conn.execute("COMMIT;")
            return {
                "status": "failed",
                "client_message_id": client_message_id,
                "conversation_id": conversation_id,
                "user_message_id": user_message_id,
                "response_message_id": resp_msg_id,
                "assistant_message": assistant_msg,
                "error_detail": error_detail,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_request_status(
        self,
        client_message_id: str,
        conversation_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Devuelve el estado transaccional de una solicitud (`processing`, `completed` o `failed`) y su respuesta vinculada."""
        if not client_message_id:
            return None
        with self._get_connection() as conn:
            if conversation_id:
                req_row = conn.execute(
                    "SELECT * FROM chat_requests WHERE client_message_id = ? AND conversation_id = ? LIMIT 1;",
                    (client_message_id, conversation_id)
                ).fetchone()
            else:
                req_row = conn.execute(
                    "SELECT * FROM chat_requests WHERE client_message_id = ? LIMIT 1;",
                    (client_message_id,)
                ).fetchone()

            if req_row is None:
                return None

            assistant_msg = None
            resp_msg_id = req_row["response_message_id"]
            user_msg_id = req_row["user_message_id"]
            if resp_msg_id:
                asst_row = conn.execute(
                    "SELECT * FROM messages WHERE message_id = ? LIMIT 1;",
                    (resp_msg_id,)
                ).fetchone()
                if asst_row:
                    assistant_msg = self._parse_message_row(asst_row)
            if assistant_msg is None:
                asst_row = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE sender = 'assistant'
                      AND (request_client_message_id = ? OR reply_to_message_id = ?)
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1;
                    """,
                    (client_message_id, user_msg_id)
                ).fetchone()
                if asst_row:
                    assistant_msg = self._parse_message_row(asst_row)

            return {
                "client_message_id": req_row["client_message_id"],
                "conversation_id": req_row["conversation_id"],
                "user_message_id": user_msg_id,
                "response_message_id": assistant_msg["message_id"] if assistant_msg else resp_msg_id,
                "status": req_row["status"],
                "error_detail": req_row["error_detail"],
                "assistant_message": assistant_msg,
                "created_at": req_row["created_at"],
                "updated_at": req_row["updated_at"],
            }

    def add_message(
        self,
        conversation_id: str,
        sender: str,
        content: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        request_client_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Inserta un mensaje de manera transaccional con soporte de vinculación explícita
        (`reply_to_message_id` y `request_client_message_id`).
        Si `client_message_id` ya existe en un mensaje de usuario, retorna el registro existente.
        """
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        self.get_or_create_conversation(conversation_id=conversation_id, user="jaime", title="Nueva Conversación")

        tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        tr_json = json.dumps(tool_results, ensure_ascii=False) if tool_results else None
        effective_req_client_id = request_client_message_id or (client_message_id if sender == "user" else None)

        with self._get_connection() as conn:
            if client_message_id:
                cur = conn.execute(
                    "SELECT * FROM messages WHERE client_message_id = ?",
                    (client_message_id,)
                )
                dup = cur.fetchone()
                if dup:
                    logger.info(f"Mensaje duplicado prevenido por client_message_id='{client_message_id}'")
                    return self._parse_message_row(dup)

            # Si es un mensaje de asistente sin reply_to_message_id explícito pero con request_client_message_id,
            # resolver el user_message_id original para garantizar el vínculo explícito.
            if sender == "assistant" and not reply_to_message_id and effective_req_client_id:
                u_row = conn.execute(
                    "SELECT message_id FROM messages WHERE client_message_id = ? LIMIT 1;",
                    (effective_req_client_id,)
                ).fetchone()
                if u_row:
                    reply_to_message_id = u_row["message_id"]

            msg_id = f"msg_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO messages (
                    message_id, conversation_id, sender, content,
                    tool_calls, tool_results, task_id, client_message_id,
                    reply_to_message_id, request_client_message_id,
                    created_at, created_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                msg_id, conversation_id, sender, content,
                tc_json, tr_json, task_id, client_message_id,
                reply_to_message_id, effective_req_client_id,
                now, now_iso
            ))

            if sender == "user" and client_message_id:
                conn.execute("""
                    INSERT OR IGNORE INTO chat_requests (
                        client_message_id, conversation_id, user_message_id,
                        response_message_id, status, error_detail, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, 'processing', NULL, ?, ?)
                """, (client_message_id, conversation_id, msg_id, now, now))
            elif sender == "assistant" and effective_req_client_id:
                conn.execute("""
                    UPDATE chat_requests
                    SET status = 'completed',
                        response_message_id = ?,
                        error_detail = NULL,
                        updated_at = ?
                    WHERE client_message_id = ?
                """, (msg_id, now, effective_req_client_id))

            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id)
            )
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass

            return {
                "message_id": msg_id,
                "conversation_id": conversation_id,
                "sender": sender,
                "role": sender,
                "content": content,
                "tool_calls": tool_calls,
                "tools_called": tool_calls,
                "tool_results": tool_results,
                "task_id": task_id,
                "client_message_id": client_message_id,
                "reply_to_message_id": reply_to_message_id,
                "request_client_message_id": effective_req_client_id,
                "created_at": now,
                "created_iso": now_iso
            }

    def save_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tools_called: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        client_message_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        request_client_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Alias para compatibilidad con enrutador."""
        return self.add_message(
            conversation_id=conversation_id,
            sender=role,
            content=content,
            tool_calls=tools_called,
            task_id=task_id,
            client_message_id=client_message_id,
            reply_to_message_id=reply_to_message_id,
            request_client_message_id=request_client_message_id
        )

    @staticmethod
    def _parse_message_row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["role"] = d.get("sender")
        if d.get("tool_calls"):
            try:
                d["tool_calls"] = json.loads(d["tool_calls"])
                d["tools_called"] = d["tool_calls"]
            except Exception:
                d["tools_called"] = []
        else:
            d["tools_called"] = []
        if d.get("tool_results"):
            try:
                d["tool_results"] = json.loads(d["tool_results"])
            except Exception:
                pass
        return d

    def get_message_by_client_id(self, client_message_id: str) -> Optional[Dict[str, Any]]:
        """Busca un mensaje por su client_message_id para deduplicación."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM messages WHERE client_message_id = ?", (client_message_id,))
            row = cur.fetchone()
            if row:
                return self._parse_message_row(row)
            return None

    def get_response_for_client_message_id(
        self,
        client_message_id: str,
        conversation_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Recupera de forma determinista la respuesta del asistente vinculada explícitamente
        al mensaje de usuario con `client_message_id`.
        1. Prioriza el vínculo transaccional en `chat_requests.response_message_id`.
        2. Prioriza las columnas explícitas `request_client_message_id` y `reply_to_message_id` en `messages`.
        3. Mantiene fallback posicional únicamente para registros históricos previos a la migración.
        """
        if not client_message_id:
            return None
        with self._get_connection() as conn:
            # 1. Buscar primero en la tabla transaccional chat_requests
            req_status = self.get_request_status(client_message_id, conversation_id=conversation_id)
            if req_status and req_status.get("assistant_message"):
                return req_status["assistant_message"]

            # 2. Buscar el mensaje de usuario original
            if conversation_id:
                user_row = conn.execute(
                    "SELECT rowid, message_id, conversation_id, created_at FROM messages WHERE client_message_id = ? AND conversation_id = ? LIMIT 1",
                    (client_message_id, conversation_id)
                ).fetchone()
            else:
                user_row = conn.execute(
                    "SELECT rowid, message_id, conversation_id, created_at FROM messages WHERE client_message_id = ? LIMIT 1",
                    (client_message_id,)
                ).fetchone()

            if not user_row:
                return None

            u_msg_id = user_row["message_id"]
            u_rowid = user_row["rowid"]
            u_conv = user_row["conversation_id"]
            u_created = user_row["created_at"]

            # 3. Buscar por vínculo explícito (reply_to_message_id o request_client_message_id)
            explicit_asst = conn.execute("""
                SELECT * FROM messages
                WHERE conversation_id = ? AND sender = 'assistant'
                  AND (reply_to_message_id = ? OR request_client_message_id = ?)
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
            """, (u_conv, u_msg_id, client_message_id)).fetchone()
            if explicit_asst:
                return self._parse_message_row(explicit_asst)

            # 4. Fallback posicional solo para mensajes históricos sin reply_to_message_id
            next_user_row = conn.execute("""
                SELECT rowid, created_at FROM messages
                WHERE conversation_id = ? AND sender = 'user'
                  AND (created_at > ? OR (created_at = ? AND rowid > ?))
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
            """, (u_conv, u_created, u_created, u_rowid)).fetchone()

            if next_user_row:
                assistant_row = conn.execute("""
                    SELECT * FROM messages
                    WHERE conversation_id = ? AND sender = 'assistant'
                      AND reply_to_message_id IS NULL
                      AND (created_at > ? OR (created_at = ? AND rowid > ?))
                      AND (created_at < ? OR (created_at = ? AND rowid < ?))
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                """, (
                    u_conv,
                    u_created, u_created, u_rowid,
                    next_user_row["created_at"], next_user_row["created_at"], next_user_row["rowid"]
                )).fetchone()
            else:
                assistant_row = conn.execute("""
                    SELECT * FROM messages
                    WHERE conversation_id = ? AND sender = 'assistant'
                      AND reply_to_message_id IS NULL
                      AND (created_at > ? OR (created_at = ? AND rowid > ?))
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                """, (u_conv, u_created, u_created, u_rowid)).fetchone()

            if assistant_row:
                return self._parse_message_row(assistant_row)
            return None

    def get_messages(self, conversation_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Obtiene la lista cronológica de mensajes de una conversación."""
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT message_id, conversation_id, sender, content,
                       tool_calls, tool_results, task_id, client_message_id,
                       reply_to_message_id, request_client_message_id,
                       created_at, created_iso
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
            """, (conversation_id, limit))
            return [self._parse_message_row(r) for r in cur.fetchall()]

    def get_recent_context_for_llm(self, conversation_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Devuelve los últimos turnos más recientes en orden cronológico [{'role': ..., 'content': ...}] para el LLM."""
        max_messages = max(1, limit * 2)
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT sender, content
                FROM (
                    SELECT sender, content, created_at, rowid
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, rowid ASC
            """, (conversation_id, max_messages))
            rows = cur.fetchall()
        llm_msgs = []
        for m in rows:
            role = "user" if m["sender"] == "user" else "assistant"
            llm_msgs.append({"role": role, "content": m["content"]})
        return llm_msgs

    def delete_conversation(self, conversation_id: str, user: str = "jaime") -> bool:
        """Elimina una conversación y sus mensajes asociados en cascada."""
        owner_email = self._owner_email()
        with self._get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ? AND (user = ? OR user IN ('jaime', ?, 'api_client'))",
                (conversation_id, user, owner_email)
            )
            conn.commit()
            return cur.rowcount > 0


chat_history_db = ChatHistoryDB()
