"""
Sistema de Aprobación Humana (Human-in-the-Loop) para Yedid Smart Home.
Permite clasificar acciones por nivel de riesgo y retener acciones de ALTO RIESGO
hasta que Jaime las autorice explícitamente desde la PWA o mediante token seguro.
"""
import json
import time
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from .audit import log_event, redact_sensitive

APPROVALS_FILE = Path("C:/Yedid-Smart-Home/data/approvals.json")
APPROVAL_TIMEOUT_SECONDS = 600  # 10 minutos de validez

def canonical_params_hash(action: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Calcula un hash criptográfico SHA-256 canónico para vincular la aprobación a la acción y parámetros exactos."""
    safe_params = redact_sensitive(params or {})
    serialized = json.dumps(safe_params, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    raw = f"{action.strip().lower()}:{serialized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class ApprovalManager:
    def __init__(self, storage_path: Path = APPROVALS_FILE):
        self.storage_path = storage_path
        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        try:
            if self.storage_path.exists():
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                now = time.time()
                # Filtrar solicitudes resueltas recientes o no expiradas
                self._approvals = {
                    k: v for k, v in data.items()
                    if v.get("status") in ("APPROVED", "REJECTED", "CONSUMED") or (now - v.get("created_at", 0) < APPROVAL_TIMEOUT_SECONDS)
                }
        except Exception as e:
            print(f"[APPROVALS ERROR] Error cargando aprobaciones: {e}")
            self._approvals = {}

    def _save(self):
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(json.dumps(self._approvals, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[APPROVALS ERROR] Error guardando aprobaciones: {e}")

    def _enrich_record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Asegura que cada solicitud exponga claramente tarea, agente, alcance (scope) y nivel de riesgo."""
        params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
        risk = (rec.get("risk_level") or "HIGH").upper()
        task_id = rec.get("task_id") or params.get("task_id") or "N/A"
        agent = rec.get("agent") or params.get("agent") or params.get("target_agent") or rec.get("requester") or "gemini_cli"
        scope = (
            rec.get("scope")
            or params.get("project_path")
            or params.get("allowed_project_path")
            or params.get("cwd")
            or "C:\\Yedid-Smart-Home"
        )
        instruction = rec.get("instruction") or params.get("instruction") or rec.get("description") or ""
        risk_label = (
            "ALTO RIESGO (HITL) — Modificación de código / Ejecución en directorio de proyecto"
            if risk == "HIGH"
            else "BAJO RIESGO — Diagnóstico supervisado"
        )
        rec["task_id"] = task_id
        rec["agent"] = agent
        rec["scope"] = str(scope)
        rec["instruction"] = instruction
        rec["risk_level"] = risk
        rec["risk_label"] = risk_label
        return rec

    def create_request(
        self,
        action: str,
        description: str,
        params: Dict[str, Any],
        risk_level: str = "HIGH",
        requester: str = "mcp_gateway"
    ) -> Dict[str, Any]:
        """Crea una solicitud de aprobación humana pendiente con huella criptográfica de parámetros."""
        self._load()
        approval_id = f"appr_{uuid.uuid4().hex[:10]}"
        now = time.time()
        p_hash = canonical_params_hash(action, params)
        safe_params = redact_sensitive(params)

        record = {
            "id": approval_id,
            "action": action.strip().lower(),
            "description": description,
            "task_id": safe_params.get("task_id") or "N/A",
            "agent": safe_params.get("agent") or safe_params.get("target_agent") or requester,
            "scope": str(safe_params.get("project_path") or safe_params.get("allowed_project_path") or safe_params.get("cwd") or "C:\\Yedid-Smart-Home"),
            "instruction": safe_params.get("instruction") or description,
            "params": safe_params,
            "params_hash": p_hash,
            "risk_level": risk_level.upper(),
            "requester": requester,
            "status": "PENDING",
            "created_at": now,
            "created_iso": datetime.now(timezone.utc).isoformat(),
            "expires_at": now + APPROVAL_TIMEOUT_SECONDS,
            "approved_by": None,
            "resolved_at": None,
            "consumed_at": None
        }
        self._enrich_record(record)
        self._approvals[approval_id] = record
        self._save()

        log_event("human_approval_requested", {
            "approval_id": approval_id,
            "action": action,
            "task_id": record["task_id"],
            "scope": record["scope"],
            "risk_level": risk_level,
            "requester": requester,
            "params_hash": p_hash
        })
        return record

    def get_pending(self) -> List[Dict[str, Any]]:
        """Devuelve todas las solicitudes pendientes activas con detalle de tarea, alcance y riesgo."""
        self._load()
        now = time.time()
        pending = []
        for aid, rec in list(self._approvals.items()):
            if rec.get("status") == "PENDING":
                if now > rec.get("expires_at", 0):
                    rec["status"] = "EXPIRED"
                else:
                    pending.append(self._enrich_record(rec))
        self._save()
        return pending

    def get_request(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene una solicitud específica evaluando expiración."""
        self._load()
        rec = self._approvals.get(approval_id)
        if rec and rec.get("status") == "PENDING" and (time.time() > rec.get("expires_at", 0)):
            rec["status"] = "EXPIRED"
            self._save()
        return self._enrich_record(rec) if rec else None

    def approve(self, approval_id: str, approver: str = "jaime") -> bool:
        """Autoriza una solicitud pendiente comprobando vigencia temporal e identidad."""
        self._load()
        rec = self.get_request(approval_id)
        if not rec or rec.get("status") != "PENDING":
            return False

        if time.time() > rec.get("expires_at", 0):
            rec["status"] = "EXPIRED"
            self._save()
            log_event("human_approval_expired_on_action", {"approval_id": approval_id})
            return False

        rec["status"] = "APPROVED"
        rec["approved_by"] = approver
        rec["resolved_at"] = time.time()
        self._save()

        log_event("human_approval_granted", {
            "approval_id": approval_id,
            "action": rec.get("action"),
            "approver": approver
        })
        return True

    def reject(self, approval_id: str, approver: str = "jaime") -> bool:
        """Rechaza una solicitud pendiente."""
        self._load()
        rec = self.get_request(approval_id)
        if not rec or rec.get("status") != "PENDING":
            return False
        rec["status"] = "REJECTED"
        rec["approved_by"] = approver
        rec["resolved_at"] = time.time()
        self._save()

        log_event("human_approval_denied", {
            "approval_id": approval_id,
            "action": rec.get("action"),
            "approver": approver
        })
        return True

    def is_action_authorized(
        self,
        approval_id: Optional[str],
        expected_action: Optional[str] = None,
        expected_params: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Verifica si una acción cuenta con aprobación humana válida, vigente, de un solo uso
        y estrictamente vinculada a la operación y parámetros exactos solicitados.
        """
        if not approval_id:
            return False
        self._load()
        rec = self._approvals.get(approval_id)
        if not rec:
            log_event("approval_check_unknown_id", {"approval_id": approval_id})
            return False

        # 1. Comprobar reuso / replay attack
        if rec.get("status") == "CONSUMED":
            log_event("approval_reuse_attempt_blocked", {
                "approval_id": approval_id,
                "consumed_at": rec.get("consumed_at"),
                "action": rec.get("action")
            })
            return False

        # 2. Comprobar estado aprobado
        if rec.get("status") != "APPROVED":
            log_event("approval_not_approved_blocked", {"approval_id": approval_id, "status": rec.get("status")})
            return False

        # 3. Comprobar expiración
        now = time.time()
        if now > rec.get("expires_at", 0):
            rec["status"] = "EXPIRED"
            self._save()
            log_event("approval_expired_blocked", {"approval_id": approval_id, "expired_at": rec.get("expires_at")})
            return False

        # 4. Comprobar vinculación exacta con la acción esperada
        if expected_action:
            clean_expected_action = expected_action.strip().lower()
            if rec.get("action") != clean_expected_action:
                log_event("approval_action_mismatch_blocked", {
                    "approval_id": approval_id,
                    "expected_action": clean_expected_action,
                    "recorded_action": rec.get("action")
                })
                return False

        # 5. Comprobar vinculación exacta con los parámetros (evitar parameter tampering)
        if expected_params is not None:
            calc_hash = canonical_params_hash(rec.get("action", ""), expected_params)
            if rec.get("params_hash") != calc_hash:
                log_event("approval_params_tampered_blocked", {
                    "approval_id": approval_id,
                    "expected_hash": calc_hash,
                    "recorded_hash": rec.get("params_hash")
                })
                return False

        # 6. Consumir token inmediatamente (Garantía de uso único / Single-Use Token)
        rec["status"] = "CONSUMED"
        rec["consumed_at"] = now
        self._save()

        log_event("human_approval_consumed", {
            "approval_id": approval_id,
            "action": rec.get("action"),
            "approved_by": rec.get("approved_by")
        })
        return True

approval_manager = ApprovalManager()
