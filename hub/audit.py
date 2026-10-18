"""Registro de auditoría local seguro con redacción estricta de credenciales."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from .config import AUDIT_LOG_FILE

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
        sanitized_details = redact_sensitive(details)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "caller": caller,
            "details": sanitized_details
        }
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[AUDIT ERROR] No se pudo escribir registro: {e}")
