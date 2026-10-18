"""Capa de Base de Datos SQLite con FTS5 y Cifrado para Yedid Memory Engine v2.2.

Garantiza:
1. Almacenamiento seguro en data/memory.db.
2. Cifrado en reposo para recuerdos 'personal' y 'muy_privado'.
3. Indexación contextual FTS5 sin fuga de datos sensibles en disco.
4. Operaciones atómicas de inserción, actualización, revisión y borrado.
"""
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DATA_DIR
from .memory_crypto import memory_crypto
from .audit import log_event

logger = logging.getLogger("yedid_memory_db")

DB_PATH = DATA_DIR / "memory.db"

# Categorías autorizadas según especificación v2.2
ALLOWED_CATEGORIES = {
    "familia", "salud", "finanzas", "trabajo", "estudios",
    "relaciones", "preferencias", "proyectos", "general", "hogar",
    "objetivos", "intereses", "tecnica"
}

ALLOWED_ACCESS_LEVELS = {"general", "personal", "muy_privado"}
ALLOWED_STATUSES = {"PENDING_REVIEW", "ACTIVE", "DISCARDED", "ARCHIVED"}

SPANISH_STOPWORDS = {
    "de", "la", "el", "en", "un", "una", "unos", "unas", "es", "son",
    "que", "por", "para", "con", "los", "las", "del", "al", "se", "su",
    "sus", "como", "mas", "pero", "este", "esta", "estos", "estas",
    "cual", "cuales", "donde", "cuando", "quien", "quienes", "cuanto",
    "cuanta", "cuantos", "cuantas"
}

class MemoryDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Obtiene conexión SQLite con WAL y foreign keys."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self):
        """Inicializa las tablas y el índice FTS5."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Tabla principal de recuerdos
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    access_level TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT,
                    content_encrypted BLOB,
                    as_of TEXT,
                    validity TEXT,
                    origin TEXT,
                    review_required INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'PENDING_REVIEW',
                    created_at REAL,
                    updated_at REAL,
                    approved_at REAL,
                    approved_by TEXT
                );
            """)

            # Tabla virtual FTS5 para búsquedas contextuales rápidas
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    id UNINDEXED,
                    title,
                    content,
                    category UNINDEXED,
                    tokenize = 'porter unicode61'
                );
            """)

            conn.commit()

        # Restringir permisos de archivo a nivel de Windows
        try:
            if os.name == "nt":
                # Asegurar que solo el usuario actual de Windows tenga acceso
                import subprocess
                user = os.environ.get("USERNAME", "jaime")
                subprocess.run(
                    f'icacls "{self.db_path}" /inheritance:r /grant:r "{user}:(F)"',
                    shell=True, capture_output=True, text=True
                )
        except Exception as e:
            logger.warning(f"No se pudieron aplicar ACLs estrictas en SQLite: {e}")

    def insert_memory(
        self,
        mem_id: str,
        category: str,
        access_level: str,
        title: str,
        content: str,
        as_of: Optional[str] = None,
        validity: Optional[str] = "permanent",
        origin: Optional[str] = "seed_import",
        review_required: bool = True,
        status: str = "PENDING_REVIEW"
    ) -> Dict[str, Any]:
        """Inserta un nuevo recuerdo aplicando cifrado en reposo según su nivel de acceso."""
        clean_cat = category.lower().strip()
        clean_level = access_level.lower().strip()
        clean_status = status.upper().strip()

        if clean_cat not in ALLOWED_CATEGORIES:
            raise ValueError(f"Categoría '{clean_cat}' no permitida. Válidas: {list(ALLOWED_CATEGORIES)}")
        if clean_level not in ALLOWED_ACCESS_LEVELS:
            raise ValueError(f"Nivel '{clean_level}' no permitido. Válidos: {list(ALLOWED_ACCESS_LEVELS)}")
        if clean_status not in ALLOWED_STATUSES:
            raise ValueError(f"Estado '{clean_status}' no permitido. Válidos: {list(ALLOWED_STATUSES)}")

        now = time.time()

        # Cifrado condicional: si es personal o muy_privado, el contenido se cifra con AES-256-GCM
        if clean_level in ("personal", "muy_privado"):
            content_plaintext = None
            content_encrypted = memory_crypto.encrypt_text(content, associated_data=mem_id)
            # Para el índice FTS5 en disco, solo indexamos el título para evitar fuga de texto sensible
            fts_content = ""
        else:
            content_plaintext = content
            content_encrypted = None
            fts_content = content

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO memories (
                    id, category, access_level, title, content, content_encrypted,
                    as_of, validity, origin, review_required, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                mem_id, clean_cat, clean_level, title, content_plaintext, content_encrypted,
                as_of, validity, origin, 1 if review_required else 0, clean_status,
                now, now
            ))

            # Sincronizar índice FTS5 (eliminar entrada previa si existía para evitar duplicados)
            cursor.execute("DELETE FROM memories_fts WHERE id = ?;", (mem_id,))
            cursor.execute("""
                INSERT INTO memories_fts (id, title, content, category)
                VALUES (?, ?, ?, ?);
            """, (mem_id, title, fts_content, clean_cat))

            conn.commit()

        log_event("memory_record_inserted", {
            "id": mem_id,
            "category": clean_cat,
            "access_level": clean_level,
            "status": clean_status,
            "encrypted": content_encrypted is not None
        })

        return self.get_memory_by_id(mem_id, decrypt=True)

    def get_memory_by_id(self, mem_id: str, decrypt: bool = True) -> Optional[Dict[str, Any]]:
        """Obtiene un recuerdo por su ID, descifrando el contenido si se solicita."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM memories WHERE id = ?;", (mem_id,))
            row = cursor.fetchone()
            if not row:
                return None

            data = dict(row)
            if decrypt and data["access_level"] in ("personal", "muy_privado"):
                enc_payload = data.get("content_encrypted")
                if enc_payload:
                    try:
                        data["content"] = memory_crypto.decrypt_text(enc_payload, associated_data=mem_id)
                    except Exception as e:
                        logger.error(f"Error descifrando memoria '{mem_id}': {e}")
                        data["content"] = "[ERROR_DESCIFRADO]"
                else:
                    data["content"] = ""
            elif not decrypt and data["access_level"] in ("personal", "muy_privado"):
                data["content"] = "[ENCRYPTED_AT_REST]"

            # Omitir el blob binario en la respuesta
            data.pop("content_encrypted", None)
            return data

    def update_status(self, mem_id: str, new_status: str, approved_by: str = "jaime") -> bool:
        """Actualiza el estado de un recuerdo (aprobación, descarte, etc.)."""
        clean_status = new_status.upper().strip()
        if clean_status not in ALLOWED_STATUSES:
            raise ValueError(f"Estado '{clean_status}' no válido.")

        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE memories
                SET status = ?, updated_at = ?, approved_at = ?, approved_by = ?
                WHERE id = ?;
            """, (clean_status, now, now if clean_status == "ACTIVE" else None, approved_by, mem_id))
            conn.commit()
            updated = cursor.rowcount > 0

        if updated:
            log_event("memory_status_updated", {"id": mem_id, "status": clean_status, "by": approved_by})
        return updated

    def update_memory(
        self,
        mem_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
        access_level: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Actualiza los campos de un recuerdo y mantiene la sincronización criptográfica y FTS5."""
        existing = self.get_memory_by_id(mem_id, decrypt=True)
        if not existing:
            return None

        new_title = title if title is not None else existing["title"]
        new_content = content if content is not None else existing["content"]
        new_category = category.lower().strip() if category is not None else existing["category"]
        new_level = access_level.lower().strip() if access_level is not None else existing["access_level"]

        if new_category not in ALLOWED_CATEGORIES:
            raise ValueError(f"Categoría '{new_category}' no permitida.")
        if new_level not in ALLOWED_ACCESS_LEVELS:
            raise ValueError(f"Nivel '{new_level}' no permitido.")

        now = time.time()

        if new_level in ("personal", "muy_privado"):
            content_plaintext = None
            content_encrypted = memory_crypto.encrypt_text(new_content, associated_data=mem_id)
            fts_content = ""
        else:
            content_plaintext = new_content
            content_encrypted = None
            fts_content = new_content

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE memories
                SET title = ?, content = ?, content_encrypted = ?, category = ?,
                    access_level = ?, updated_at = ?
                WHERE id = ?;
            """, (new_title, content_plaintext, content_encrypted, new_category, new_level, now, mem_id))

            # Actualizar FTS5
            cursor.execute("DELETE FROM memories_fts WHERE id = ?;", (mem_id,))
            cursor.execute("""
                INSERT INTO memories_fts (id, title, content, category)
                VALUES (?, ?, ?, ?);
            """, (mem_id, new_title, fts_content, new_category))

            conn.commit()

        log_event("memory_record_updated", {"id": mem_id, "category": new_category, "access_level": new_level})
        return self.get_memory_by_id(mem_id, decrypt=True)

    def delete_memory(self, mem_id: str) -> bool:
        """Elimina un recuerdo definitivamente tanto de la tabla como del índice FTS5."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories WHERE id = ?;", (mem_id,))
            deleted = cursor.rowcount > 0
            cursor.execute("DELETE FROM memories_fts WHERE id = ?;", (mem_id,))
            conn.commit()

        if deleted:
            log_event("memory_record_deleted", {"id": mem_id})
        return deleted

    def list_memories(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        access_level: Optional[str] = None,
        decrypt: bool = True
    ) -> List[Dict[str, Any]]:
        """Lista recuerdos filtrando por estado, categoría y nivel de acceso."""
        query = "SELECT * FROM memories WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status.upper().strip())
        if category:
            query += " AND category = ?"
            params.append(category.lower().strip())
        if access_level:
            query += " AND access_level = ?"
            params.append(access_level.lower().strip())

        query += " ORDER BY created_at DESC;"

        results = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

            for r in rows:
                item = dict(r)
                if decrypt and item["access_level"] in ("personal", "muy_privado"):
                    enc = item.get("content_encrypted")
                    if enc:
                        try:
                            item["content"] = memory_crypto.decrypt_text(enc, associated_data=item["id"])
                        except Exception:
                            item["content"] = "[ERROR_DESCIFRADO]"
                    else:
                        item["content"] = ""
                elif not decrypt and item["access_level"] in ("personal", "muy_privado"):
                    item["content"] = "[ENCRYPTED_AT_REST]"

                item.pop("content_encrypted", None)
                results.append(item)

        return results

    def search_fts(self, query: str, limit: int = 10) -> List[str]:
        """Ejecuta búsqueda rápida de texto completo devolviendo los IDs coincidentes."""
        clean_q = re.sub(r"[^\w\s]", " ", query).strip()
        if not clean_q:
            return []

        # Formatear tokens para match FTS5 excluyendo stop words y palabras muy cortas
        tokens = [
            f'"{tok}"*' for tok in clean_q.lower().split()
            if len(tok) >= 3 and tok not in SPANISH_STOPWORDS
        ]
        if not tokens:
            return []

        fts_match = " OR ".join(tokens)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT id, rank FROM memories_fts
                    WHERE memories_fts MATCH ?
                    ORDER BY rank LIMIT ?;
                """, (fts_match, limit))
                rows = cursor.fetchall()
                return [r["id"] for r in rows]
            except Exception as e:
                logger.warning(f"Error en FTS5 match '{fts_match}': {e}")
                return []

    def get_summary_stats(self) -> Dict[str, Any]:
        """Estadísticas globales para dashboard de memoria."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, count(*) as cnt FROM memories GROUP BY status;")
            by_status = {r["status"]: r["cnt"] for r in cursor.fetchall()}

            cursor.execute("SELECT category, count(*) as cnt FROM memories GROUP BY category;")
            by_category = {r["category"]: r["cnt"] for r in cursor.fetchall()}

            cursor.execute("SELECT access_level, count(*) as cnt FROM memories GROUP BY access_level;")
            by_level = {r["access_level"]: r["cnt"] for r in cursor.fetchall()}

            cursor.execute("SELECT count(*) as total FROM memories;")
            total = cursor.fetchone()["total"]

        return {
            "total_memories": total,
            "by_status": by_status,
            "by_category": by_category,
            "by_access_level": by_level,
            "pending_count": by_status.get("PENDING_REVIEW", 0),
            "active_count": by_status.get("ACTIVE", 0)
        }

memory_db = MemoryDB()
