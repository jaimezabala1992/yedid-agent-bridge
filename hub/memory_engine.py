"""Motor de Memoria y Privacidad para Yedid Memory Engine v2.2.

Coordina:
1. Importación estructurada de archivos esquema yedid_memory_seed/1.0.
2. Flujo de aprobación humana previa (Human-in-the-Loop) antes de activar recuerdos.
3. Control estricto de acceso por roles: general, personal, muy_privado.
4. Confinamiento de agentes: MCP genérico y agentes externos jamás acceden a personal/muy_privado.
5. Inyección contextual mínima y filtrada para el LLM local.
6. Cero fugas en auditoría y cero costos de nube ($0.00).
"""
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from .memory_db import (
    memory_db,
    ALLOWED_CATEGORIES,
    ALLOWED_ACCESS_LEVELS,
    ALLOWED_STATUSES,
    SPANISH_STOPWORDS
)
from .config import AUTH_TOKEN
from .audit import log_event

logger = logging.getLogger("yedid_memory_engine")

# Tiempo de validez del token de step-up para ver recuerdos 'muy_privado' (15 minutos)
STEPUP_TOKEN_LIFETIME = 900.0

class MemoryEngine:
    def __init__(self):
        # Almacena tokens de step-up temporales {token: expires_at}
        self._stepup_tokens: Dict[str, float] = {}

    def create_stepup_session(self, secret: str) -> Optional[str]:
        """Valida la credencial del propietario y genera un token temporal de step-up."""
        from .config import get_auth_token
        current_token = get_auth_token()
        if not secret or secret.strip() != current_token:
            log_event("memory_stepup_failed", {"reason": "invalid_credentials"})
            return None

        token = f"stepup_{secrets.token_hex(16)}"
        expires_at = time.time() + STEPUP_TOKEN_LIFETIME
        self._stepup_tokens[token] = expires_at
        log_event("memory_stepup_success", {"expires_in_seconds": STEPUP_TOKEN_LIFETIME})
        return token

    def is_stepup_valid(self, token: Optional[str]) -> bool:
        """Comprueba si un token de step-up está activo y no ha expirado."""
        if not token or token not in self._stepup_tokens:
            return False
        if time.time() > self._stepup_tokens[token]:
            del self._stepup_tokens[token]
            return False
        return True

    def revoke_stepup_token(self, token: Optional[str]) -> bool:
        """Revoca inmediatamente un token de step-up activo eliminándolo de la memoria volátil."""
        if token and token in self._stepup_tokens:
            del self._stepup_tokens[token]
            log_event("memory_stepup_revoked", {})
            return True
        return False


    def import_seed_json(self, seed_data: Dict[str, Any], source: str = "pwa_upload") -> Dict[str, Any]:
        """Importa un archivo con esquema yedid_memory_seed/1.0.
        
        Garantiza que la importación se ejecute de forma 100% determinista y local,
        sin intervención de ningún LLM ni lectura de contenido por agentes externos.
        Todos los registros se crean en estado 'PENDING_REVIEW' para revisión del propietario.
        """
        # 1. Validación de esquema
        schema = seed_data.get("schema", "")
        if schema != "yedid_memory_seed/1.0":
            raise ValueError(f"Esquema no soportado: '{schema}'. Se requiere 'yedid_memory_seed/1.0'.")

        memories_list = seed_data.get("memories", [])
        if not isinstance(memories_list, list) or len(memories_list) == 0:
            raise ValueError("El archivo no contiene un arreglo 'memories' válido o está vacío.")

        imported_records = []
        rejected_records = []
        categories_count: Dict[str, int] = {}
        levels_count: Dict[str, int] = {}

        for idx, item in enumerate(memories_list):
            try:
                mem_id = item.get("id")
                if not mem_id or not isinstance(mem_id, str):
                    mem_id = f"mem_{secrets.token_hex(6)}"

                category = item.get("category", "").lower().strip()
                if category not in ALLOWED_CATEGORIES:
                    raise ValueError(f"Categoría '{category}' no válida.")

                access_level = item.get("access_level", "").lower().strip()
                if access_level not in ALLOWED_ACCESS_LEVELS:
                    raise ValueError(f"Nivel de acceso '{access_level}' no válido.")

                title = item.get("title", "").strip()
                if not title or len(title) > 200:
                    raise ValueError("El título es obligatorio y debe tener máximo 200 caracteres.")

                content = item.get("content", "").strip()
                if not content or len(content) > 10000:
                    raise ValueError("El contenido es obligatorio y debe tener máximo 10,000 caracteres.")

                as_of = item.get("as_of", "")
                validity = item.get("validity", "permanent")
                origin = item.get("origin", "seed_import")
                review_required = bool(item.get("review_required", True))

                # Insertar en base de datos en estado PENDING_REVIEW (cifrado automático si es sensible)
                rec = memory_db.insert_memory(
                    mem_id=mem_id,
                    category=category,
                    access_level=access_level,
                    title=title,
                    content=content,
                    as_of=as_of,
                    validity=validity,
                    origin=origin,
                    review_required=review_required,
                    status="PENDING_REVIEW"
                )

                imported_records.append({
                    "id": mem_id,
                    "category": category,
                    "access_level": access_level,
                    "title": title,
                    "status": "PENDING_REVIEW"
                })

                categories_count[category] = categories_count.get(category, 0) + 1
                levels_count[access_level] = levels_count.get(access_level, 0) + 1

            except Exception as e:
                rejected_records.append({"index": idx, "error": str(e), "title": item.get("title", "")})

        log_event("memory_seed_batch_imported", {
            "source": source,
            "total_imported": len(imported_records),
            "total_rejected": len(rejected_records),
            "categories": categories_count,
            "levels": levels_count
        })

        return {
            "success": True,
            "total_imported": len(imported_records),
            "total_rejected": len(rejected_records),
            "categories_summary": categories_count,
            "levels_summary": levels_count,
            "imported_preview": imported_records,
            "rejected": rejected_records,
            "message": f"Se importaron {len(imported_records)} recuerdos en estado PENDING_REVIEW. Requieren revisión y aprobación humana antes de activarse."
        }

    def review_memory(
        self,
        memory_id: str,
        action: str,
        approver: str = "jaime",
        stepup_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Aprueba o descarta un recuerdo pendiente.
        
        Para recuerdos 'muy_privado', exige verificación previa de step-up.
        """
        mem = memory_db.get_memory_by_id(memory_id, decrypt=False)
        if not mem:
            return {"success": False, "error": f"Recuerdo '{memory_id}' no encontrado."}

        clean_action = action.lower().strip()
        if clean_action not in ("approve", "discard"):
            return {"success": False, "error": f"Acción '{action}' no válida. Use 'approve' o 'discard'."}

        # Verificación reforzada para recuerdos de alta sensibilidad
        if mem["access_level"] == "muy_privado" and clean_action == "approve":
            if not self.is_stepup_valid(stepup_token):
                return {
                    "success": False,
                    "requires_stepup": True,
                    "error": "La aprobación de recuerdos 'muy_privado' requiere reautenticación / step-up reciente."
                }

        target_status = "ACTIVE" if clean_action == "approve" else "DISCARDED"
        ok = memory_db.update_status(memory_id, target_status, approved_by=approver)

        return {
            "success": ok,
            "id": memory_id,
            "new_status": target_status,
            "access_level": mem["access_level"],
            "title": mem["title"]
        }

    def review_batch(
        self,
        action: str,
        category: Optional[str] = None,
        approver: str = "jaime",
        stepup_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Aprueba o descarta en lote los recuerdos pendientes."""
        pending = memory_db.list_memories(status="PENDING_REVIEW", category=category, decrypt=False)
        stepup_ok = self.is_stepup_valid(stepup_token)

        approved_count = 0
        discarded_count = 0
        skipped_very_private = 0

        clean_action = action.lower().strip()

        for item in pending:
            m_id = item["id"]
            lvl = item["access_level"]

            if lvl == "muy_privado" and clean_action == "approve" and not stepup_ok:
                skipped_very_private += 1
                continue

            target = "ACTIVE" if clean_action == "approve" else "DISCARDED"
            if memory_db.update_status(m_id, target, approved_by=approver):
                if target == "ACTIVE":
                    approved_count += 1
                else:
                    discarded_count += 1

        return {
            "success": True,
            "action": clean_action,
            "approved": approved_count,
            "discarded": discarded_count,
            "skipped_very_private": skipped_very_private,
            "remaining_pending": memory_db.get_summary_stats()["pending_count"]
        }

    def get_context_for_llm(
        self,
        user_query: str,
        caller_role: str = "local_chat",
        is_owner_authenticated: bool = True,
        stepup_token: Optional[str] = None,
        max_items: int = 3
    ) -> str:
        """Recupera contexto mínimo y relevante para inyectar en el LLM local.
        
        Aplica política de confinamiento:
        - Agentes MCP genéricos o externos: SOLO nivel 'general'.
        - Sesión local del propietario: 'general' y 'personal'.
        - Nivel 'muy_privado': Exclusivamente con step-up válido; nunca se inyecta pasivamente.
        """
        # Si la consulta es trivial o vacía, no inyectar nada
        clean_q = user_query.strip()
        if not clean_q or len(clean_q) < 3:
            return ""

        # Determinar niveles de acceso permitidos para esta llamada
        allowed_levels = ["general"]
        if is_owner_authenticated and caller_role == "local_chat":
            allowed_levels.append("personal")
            if self.is_stepup_valid(stepup_token):
                allowed_levels.append("muy_privado")

        # Buscar coincidencias mediante FTS5
        matched_ids = list(memory_db.search_fts(clean_q, limit=10))

        # Para sesión local del propietario, también contrastar términos clave contra memorias activas en memoria volátil
        if is_owner_authenticated and caller_role == "local_chat" and len(matched_ids) < max_items:
            active_mems = memory_db.list_memories(status="ACTIVE", decrypt=True)
            query_words = [w.lower() for w in re.sub(r"[^\w\s]", " ", clean_q).split() if len(w) >= 3 and w.lower() not in SPANISH_STOPWORDS]
            for am in active_mems:
                if am["id"] in matched_ids:
                    continue
                if am.get("access_level") not in allowed_levels:
                    continue
                searchable_text = f"{am.get('title', '')} {am.get('content', '')} {am.get('category', '')}".lower()
                if any(qw in searchable_text for qw in query_words):
                    matched_ids.append(am["id"])
                    if len(matched_ids) >= 10:
                        break

        if not matched_ids:
            return ""

        snippets = []
        for m_id in matched_ids:
            mem = memory_db.get_memory_by_id(m_id, decrypt=True)
            if not mem:
                continue
            # Solo recuerdos aprobados y activos
            if mem.get("status") != "ACTIVE":
                continue
            # Filtrar por política de acceso
            if mem.get("access_level") not in allowed_levels:
                continue

            content_text = mem.get("content", "").strip()
            cat = mem.get("category", "general")
            title = mem.get("title", "")
            snippets.append(f"- [{cat.upper()}] {title}: {content_text}")

            if len(snippets) >= max_items:
                break

        if not snippets:
            return ""

        context_block = (
            "\n[INFORMACIÓN DE MEMORIA PERSONAL AUTORIZADA DE JAIME (USO LOCAL EXCLUSIVO)]:\n"
            + "\n".join(snippets)
            + "\nUtiliza estos datos solo si responden directamente a la pregunta de Jaime de forma concisa.\n"
        )
        return context_block

memory_engine = MemoryEngine()
