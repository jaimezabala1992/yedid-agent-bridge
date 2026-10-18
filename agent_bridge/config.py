# -*- coding: utf-8 -*-
"""
agent_bridge/config.py

Configuración central y almacenamiento seguro para Yedid Agent Bridge.
- Cero almacenamiento de secretos en repositorios Git.
- Resguardo de token de GitHub fuera de rutas web y bajo NTFS ACLs estrictas.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("YedidAgentBridge.Config")

BRIDGE_ROOT = Path(__file__).parent.resolve()
BASE_DIR = BRIDGE_ROOT.parent.resolve()
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "agent_bridge_config.json"
TOKEN_FILE = DATA_DIR / ".github_token"
PAUSE_FILE = DATA_DIR / ".bridge_paused"
DB_PATH = DATA_DIR / "agent_bridge.db"
LOCK_FILE = DATA_DIR / ".bridge_daemon.lock"

# Valores por defecto de configuración
DEFAULT_CONFIG: Dict[str, Any] = {
    "repo_owner": "",
    "repo_name": "yedid",
    "poll_interval_seconds": 30,
    "authorized_users": ["jaime", "yedid-host"],
    "auto_label_processed": "yedid-processed",
    "auto_close_completed": False,
    "max_issues_per_poll": 10,
    "hub_url": "http://127.0.0.1:8765",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen3:8b",
    "log_level": "INFO"
}

def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR

def load_bridge_config() -> Dict[str, Any]:
    """Carga la configuración del Bridge combinando defaults con el archivo JSON."""
    ensure_data_dir()
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                config.update(user_cfg)
        except Exception as e:
            logger.warning(f"Error cargando {CONFIG_FILE}, usando defaults: {e}")
    return config

def save_bridge_config(new_config: Dict[str, Any]) -> None:
    """Guarda la configuración actualizada en data/agent_bridge_config.json."""
    ensure_data_dir()
    current = load_bridge_config()
    current.update(new_config)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    logger.info(f"Configuración guardada en {CONFIG_FILE}")

WIN_CRED_TARGET = "YedidAgentBridge:github_token"

def write_windows_credential(target: str, user: str, secret: str) -> bool:
    """Guarda una credencial cifrada en Windows Credential Manager."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32

        CRED_TYPE_GENERIC = 1
        CRED_PERSIST_LOCAL_MACHINE = 2

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.c_char_p),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR)
            ]

        blob = secret.strip().encode("utf-8")
        cred = CREDENTIAL(
            Flags=0,
            Type=CRED_TYPE_GENERIC,
            TargetName=target,
            Comment="Yedid Agent Bridge GitHub Token",
            CredentialBlobSize=len(blob),
            CredentialBlob=blob,
            Persist=CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=user
        )
        return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))
    except Exception as e:
        logger.warning(f"Error escribiendo en Windows Credential Manager: {e}")
        return False

def read_windows_credential(target: str) -> Optional[str]:
    """Lee una credencial almacenada en Windows Credential Manager."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32

        CRED_TYPE_GENERIC = 1

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.c_char_p),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR)
            ]

        PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
        pcred = PCREDENTIAL()
        if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            return None
        try:
            blob = ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize)
            return blob.decode("utf-8").strip()
        finally:
            advapi32.CredFree(pcred)
    except Exception as e:
        logger.debug(f"Credencial {target} no encontrada en Windows Credential Manager: {e}")
        return None

def get_github_token() -> Optional[str]:
    """Obtiene el token de GitHub desde Windows Credential Manager, entorno o archivo protegido.
    
    Precedencia de Seguridad:
    1. Windows Credential Manager (YedidAgentBridge:github_token)
    2. Git Credential Manager (git:https://github.com)
    3. GITHUB_TOKEN / GH_TOKEN en variables de entorno
    4. C:\\Yedid-Smart-Home\\data\\.github_token (con NTFS ACLs)
    """
    # 1. Windows Credential Manager
    win_token = read_windows_credential(WIN_CRED_TARGET)
    if win_token:
        return win_token

    # 2. Git Credential Manager
    gcm_token = read_windows_credential("git:https://github.com")
    if gcm_token:
        return gcm_token

    # 3. Variables de entorno
    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    # 4. Archivo resguardado con NTFS ACLs
    if TOKEN_FILE.exists():
        try:
            content = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception as e:
            logger.error(f"Error leyendo {TOKEN_FILE}: {e}")
            
    return None

def save_github_token(token: str, username: str = "jaimezabala1992") -> bool:
    """Guarda el token de GitHub en Windows Credential Manager y con permisos NTFS restringidos."""
    ensure_data_dir()
    clean = token.strip()
    if not clean:
        raise ValueError("El token de GitHub no puede estar vacío.")

    # 1. Guardar en Windows Credential Manager
    write_windows_credential(WIN_CRED_TARGET, username, clean)

    # 2. Guardar en archivo local con ACLs NTFS como respaldo local del servicio
    TOKEN_FILE.write_text(clean, encoding="utf-8")

    if os.name == "nt":
        try:
            import subprocess
            cmd = f'icacls "{TOKEN_FILE}" /inheritance:r /grant:r "*S-1-5-18:(F)" "*S-1-5-32-544:(F)" "%USERNAME%:(F)"'
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
            logger.info("Permisos NTFS reforzados en .github_token")
        except Exception as e:
            logger.warning(f"No se pudieron restringir ACLs de NTFS: {e}")

    return True


def is_bridge_paused() -> bool:
    """Retorna True si el Bridge está pausado por orden del usuario o la PWA."""
    return PAUSE_FILE.exists()

def set_bridge_paused(paused: bool) -> None:
    """Pausa o reanuda la operación del Bridge."""
    ensure_data_dir()
    if paused:
        PAUSE_FILE.write_text("PAUSED", encoding="utf-8")
        logger.info("Yedid Agent Bridge ha sido PAUSADO")
    else:
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink(missing_ok=True)
        logger.info("Yedid Agent Bridge ha sido REANUDADO")
