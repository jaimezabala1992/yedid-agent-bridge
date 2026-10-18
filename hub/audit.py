"""Registro de auditoría local seguro con redacción estricta de credenciales."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from .config import AUDIT_LOG_FILE, get_data_dir, is_test_mode

SENSITIVE_EXACT_KEYS = {
    "token", "auth_token", "password", "secret", "authorization",
    "cookie", "csrf_token", "x-yedid-token", "access_token", "api_key", "secret_key"
}

def _is_sensitive_key(key_name: str) -> bool:
    k = str(key_name).strip().lower()
    if k in SENSITIVE_EXACT_KEYS:
        return True
    return any(k.endswith(suffix) for suffix in ("_token", "_secret", "_password", "_api_key"))

def redact_sensitive(obj: Any) -> Any:
    """Oculta recursivamente cualquier clave o contenido sensible."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                cleaned[k] = "[REDACTADO]"
            else:
                cleaned[k] = redact_sensitive(v)
        return cleaned
    elif isinstance(obj, list):
        return [redact_sensitive(item) for item in obj]
    elif isinstance(obj, str):
        low = obj.lower()
        if "bearer " in low or "data\\.token" in low or "data/.token" in low:
            return "[REDACTADO_POSIBLE_TOKEN]"
        return obj
    return obj

def log_event(event_type: str, details: Dict[str, Any], caller: str = "local") -> None:
    """Registra una entrada estructurada en audit.jsonl garantizando anonimización de secretos."""
    try:
        if is_test_mode():
            target_log = get_data_dir() / "audit.jsonl"
            # En modo test sin YEDID_DATA_DIR explícito, no escribir al repositorio
            resolved = str(target_log.resolve()).replace("/", "\\").lower()
            repo_data = str((Path(__file__).resolve().parent.parent / "data").resolve()).replace("/", "\\").lower()
            if resolved.startswith(r"c:\yedid-smart-home\data") or resolved.startswith(repo_data):
                return
        else:
            target_log = AUDIT_LOG_FILE
        target_log.parent.mkdir(parents=True, exist_ok=True)
        sanitized_details = redact_sensitive(details)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "caller": caller,
            "details": sanitized_details
        }
        with open(target_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[AUDIT ERROR] No se pudo escribir registro: {e}")
