"""Configuración central para Yedid Hub."""
import os
from pathlib import Path

BASE_DIR = Path(os.getenv("YEDID_BASE_DIR", str(Path(__file__).resolve().parent.parent))).resolve()
DATA_DIR = Path(os.getenv("YEDID_DATA_DIR", str(BASE_DIR / "data"))).resolve()


def get_data_dir() -> Path:
    """Devuelve el directorio de datos activo (respeta YEDID_DATA_DIR en tiempo de ejecución y pruebas)."""
    env_dir = os.getenv("YEDID_DATA_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return DATA_DIR


def is_test_mode() -> bool:
    """Indica si el sistema se está ejecutando bajo suite de pruebas aisladas."""
    return os.getenv("YEDID_TEST_MODE", "0") == "1"


def assert_not_production_db_in_test(db_path: Path) -> None:
    """Bloquea cualquier intento de inicializar o modificar bases de datos de producción durante pruebas."""
    if not is_test_mode():
        return
    resolved = str(Path(db_path).resolve()).replace("/", "\\").lower()
    prod_data = r"c:\yedid-smart-home\data"
    repo_data = str((Path(__file__).resolve().parent.parent / "data").resolve()).replace("/", "\\").lower()
    if resolved.startswith(prod_data) or resolved.startswith(repo_data):
        raise RuntimeError(
            f"[TEST ISOLATION GUARD] Intento bloqueado de acceder o inicializar base de datos fuera del entorno aislado de pruebas: {db_path}"
        )


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
    if is_test_mode():
        return "test_isolated_token_ci_only"
    token_file = get_data_dir() / ".token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    import secrets
    token_file.parent.mkdir(parents=True, exist_ok=True)
    new_token = secrets.token_hex(24)
    token_file.write_text(new_token, encoding="utf-8")
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

