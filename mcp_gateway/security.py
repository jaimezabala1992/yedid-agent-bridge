"""
Módulo de seguridad, validación y auditoría para Yedid MCP Gateway.
Garantiza:
1. Cero ejecución de comandos de sistema o terminal.
2. Cero acceso no autorizado a rutas de archivos.
3. Cero devolución de secretos, contraseñas, tokens o cookies.
4. Sanitización estricta de datos provenientes de dispositivos externos o registros.
5. Límites estrictos de tamaño en respuestas y registros de auditoría obligatorios.
"""
import re
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
import sys

BASE_DIR = Path("C:/Yedid-Smart-Home")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from hub.config import AUTH_TOKEN, AUDIT_LOG_FILE
from hub.audit import redact_sensitive

# Patrón para sanitizar cadenas provenientes de dispositivos o usuarios externos
DANGEROUS_CHARS_REGEX = re.compile(r'[\x00-\x1f\x7f-\x9f<>]')

def sanitize_untrusted_string(val: Any, max_len: int = 150) -> str:
    """Limpia cadenas no confiables eliminando caracteres de control e inyecciones."""
    if val is None:
        return ""
    text = str(val).strip()
    # Eliminar caracteres de control y etiquetas HTML
    cleaned = DANGEROUS_CHARS_REGEX.sub('', text)
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned

def enforce_payload_limit(data: Any, max_bytes: int = 8192) -> Any:
    """Verifica que el payload devuelto por las herramientas no exceda el límite de bytes."""
    serialized = json.dumps(data, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > max_bytes:
        return {
            "error": "Payload truncado por límite de seguridad",
            "truncated": True
        }
    return data

def log_mcp_event(
    tool_name: str,
    arguments: Dict[str, Any],
    success: bool,
    result_summary: Any,
    client_ip: str = "127.0.0.1",
    duration_ms: float = 0.0
) -> None:
    """Registra llamadas a herramientas MCP en audit.jsonl garantizando anonimización de secretos."""
    try:
        clean_args = redact_sensitive(arguments)
        clean_result = redact_sensitive(result_summary)
        
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "mcp_tool_call",
            "caller": "mcp_gateway",
            "tool_name": tool_name,
            "client_ip": client_ip,
            "success": success,
            "duration_ms": round(duration_ms, 2),
            "arguments": clean_args,
            "result_summary": clean_result
        }
        
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[MCP AUDIT ERROR] No se pudo escribir registro: {e}", file=sys.stderr)

def authenticate_mcp_request(headers: Dict[str, str], client_host: str = "127.0.0.1") -> bool:
    """
    Verifica si una petición HTTP entrante al MCP Gateway está autorizada:
    1. Si proviene de loopback localhost (127.0.0.1 / ::1), se autoriza para clientes locales.
    2. Si incluye Authorization: Bearer <AUTH_TOKEN>, se autoriza.
    3. Si incluye header X-MCP-Token: <AUTH_TOKEN>, se autoriza.
    """
    # 1. Bearer Token
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split("Bearer ", 1)[1].strip()
        if token == AUTH_TOKEN:
            return True

    # 2. X-MCP-Token
    mcp_token = headers.get("x-mcp-token", "")
    if mcp_token and mcp_token == AUTH_TOKEN:
        return True

    # 3. Localhost directo sin proxy
    if client_host in ("127.0.0.1", "::1", "localhost") and not headers.get("x-forwarded-for"):
        return True

    return False
