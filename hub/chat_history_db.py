"""
hub/chat_history_db.py

Base de datos SQLite persistente y transaccional para el historial de conversaciones de Yedid.
- Fuente de verdad en el servidor (sobrevive a recargas, cambios de pestañas y reinicios del Hub).
- Identificadores de conversación estables (UUID) por usuario.
- Deduplicación por client_message_id (idempotencia contra reintentos de red del navegador).
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

logger = logging.getLogger("yedid_chat_db")

DATA_DIR = Path("C:/Yedid-Smart-Home/data")
DB_PATH = DATA_DIR / "chat_history.db"


class ChatHistoryDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        """Inicializa las tablas de conversaciones y mensajes con índices."""
        with self._get_connection() as conn:
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
                    created_at REAL NOT NULL,
                    created_iso TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
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
                CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user, updated_at DESC);
            """)
            conn.commit()

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

            # Crear nueva conversación
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
        """Verifica si un mensaje con este client_message_id ya fue registrado."""
        if not client_message_id:
            return False
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE client_message_id = ? LIMIT 1;",
                (client_message_id,)
            ).fetchone()
            return row is not None

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

    def add_message(
        self,
        conversation_id: str,
        sender: str,
        content: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        client_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Inserta un mensaje de manera transaccional.
        Si client_message_id ya existe, retorna el registro existente (idempotencia).
        """
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        # Asegurar que la conversación exista para satisfacer la restricción de clave foránea
        self.get_or_create_conversation(conversation_id=conversation_id, user="jaime", title="Nueva Conversación")

        tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        tr_json = json.dumps(tool_results, ensure_ascii=False) if tool_results else None

        with self._get_connection() as conn:
            # 1. Comprobar duplicado por client_message_id
            if client_message_id:
                cur = conn.execute(
                    "SELECT * FROM messages WHERE client_message_id = ?",
                    (client_message_id,)
                )
                dup = cur.fetchone()
                if dup:
                    logger.info(f"Mensaje duplicado prevenido por client_message_id='{client_message_id}'")
                    return self._parse_message_row(dup)

            msg_id = f"msg_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO messages (
                    message_id, conversation_id, sender, content,
                    tool_calls, tool_results, task_id, client_message_id,
                    created_at, created_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                msg_id, conversation_id, sender, content,
                tc_json, tr_json, task_id, client_message_id,
                now, now_iso
            ))

            # Actualizar updated_at de la conversación
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
        client_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Alias para compatibilidad con enrutador."""
        return self.add_message(
            conversation_id=conversation_id,
            sender=role,
            content=content,
            tool_calls=tools_called,
            task_id=task_id,
            client_message_id=client_message_id
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
        Encuentra el mensaje de usuario asociado a `client_message_id` y devuelve
        la respuesta exacta del asistente generada inmediatamente después en esa conversación,
        antes del siguiente mensaje de usuario.
        """
        if not client_message_id:
            return None
        with self._get_connection() as conn:
            if conversation_id:
                user_row = conn.execute(
                    "SELECT rowid, conversation_id, created_at FROM messages WHERE client_message_id = ? AND conversation_id = ? LIMIT 1",
                    (client_message_id, conversation_id)
                ).fetchone()
            else:
                user_row = conn.execute(
                    "SELECT rowid, conversation_id, created_at FROM messages WHERE client_message_id = ? LIMIT 1",
                    (client_message_id,)
                ).fetchone()

            if not user_row:
                return None

            u_rowid = user_row["rowid"]
            u_conv = user_row["conversation_id"]
            u_created = user_row["created_at"]

            # Buscar si hubo otro turno de usuario posterior para acotar el rango del turno
            next_user_row = conn.execute("""
                SELECT rowid, created_at FROM messages
                WHERE conversation_id = ? AND sender = 'user'
                  AND (created_at > ? OR (created_at = ? AND rowid > ?))
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
            """, (u_conv, u_created, u_created, u_rowid)).fetchone()

            if next_user_row:
                assistant_row = conn.execute("""
                    SELECT message_id, conversation_id, sender, content,
                           tool_calls, tool_results, task_id, client_message_id,
                           created_at, created_iso
                    FROM messages
                    WHERE conversation_id = ? AND sender = 'assistant'
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
                    SELECT message_id, conversation_id, sender, content,
                           tool_calls, tool_results, task_id, client_message_id,
                           created_at, created_iso
                    FROM messages
                    WHERE conversation_id = ? AND sender = 'assistant'
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
