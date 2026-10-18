"""Configuración central para Yedid Hub."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Red y Servidor (Estrictamente loopback local por defecto)
HOST = os.getenv("YEDID_HOST", "127.0.0.1")
PORT = int(os.getenv("YEDID_PORT", "8765"))

# Ollama local
OLLAMA_URL = os.getenv("YEDID_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("YEDID_OLLAMA_MODEL", "qwen3:8b")

# Dispositivos
TV_HOST = os.getenv("YEDID_TV_HOST", "192.168.1.100")
TV_CERT_FILE = BASE_DIR / ".tvremote" / "state" / "cert.pem"
TV_KEY_FILE = BASE_DIR / ".tvremote" / "state" / "key.pem"

# Auditoría
AUDIT_LOG_FILE = DATA_DIR / "audit.jsonl"
TOKEN_FILE = DATA_DIR / ".token"

# Autenticación opcional para accesos externos (localhost siempre permitido)
def get_or_create_token() -> str:
    env_token = os.getenv("YEDID_TOKEN")
    if env_token:
        return env_token.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    import secrets
    new_token = secrets.token_hex(24)
    TOKEN_FILE.write_text(new_token, encoding="utf-8")
    return new_token

AUTH_TOKEN = get_or_create_token()

def get_auth_token() -> str:
    """Obtiene la credencial maestra actual desde el archivo protegido o memoria."""
    env_token = os.getenv("YEDID_TOKEN")
    if env_token:
        return env_token.strip()
    if TOKEN_FILE.exists():
        try:
            val = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass
    return AUTH_TOKEN

