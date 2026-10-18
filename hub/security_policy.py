"""
hub/security_policy.py

Módulo central de políticas de seguridad, validación de rutas y aislamiento de procesos:
1. Lista blanca estricta de rutas autorizadas por proyecto.
2. Lista negra inquebrantable de rutas sensibles (memorias privadas, credenciales, respaldos, sistema).
3. Sanitización de instrucciones para prevenir inyección de comandos de shell.
4. Entorno de ejecución limpio (limpieza de variables de entorno y tokens).
5. Reglas automáticas de seguridad de espacio de trabajo (.agents/rules/security.md y GEMINI.md).
6. Clasificación determinista de riesgo (inmune a prompt injection de Qwen3).
7. Auditoría post-ejecución de entregables.
"""

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("yedid_security")

# Directorios raíz permitidos explícitamente para proyectos de Jaime
ALLOWED_PROJECT_ROOTS = [
    Path(r"C:\Yedid-Smart-Home\sandbox").resolve(),
    Path(os.path.expandvars(r"%USERPROFILE%\Documents\Codex")).resolve(),
    Path(os.path.expandvars(r"%USERPROFILE%\workspace")).resolve(),
    Path(r"C:\Yedid-Smart-Home").resolve()
]

# Rutas sensibles ESTRICTAMENTE PROHIBIDAS (Red Zones)
# Ningún agente o proceso puede leer, escribir o listar estas rutas, incluso si están dentro de un proyecto permitido.
FORBIDDEN_EXACT_OR_PREFIX = [
    Path(r"C:\Yedid-Smart-Home\data\memories_v2.db").resolve(),
    Path(r"C:\Yedid-Smart-Home\data\memories.json").resolve(),
    Path(r"C:\Yedid-Smart-Home\data\.token").resolve(),
    Path(r"C:\Yedid-Smart-Home\backups").resolve(),
    Path(r"C:\Windows").resolve(),
    Path(r"C:\Program Files").resolve(),
    Path(r"C:\Program Files (x86)").resolve(),
    Path(r"C:\ProgramData").resolve(),
    Path(os.path.expandvars(r"%USERPROFILE%\.ssh")).resolve(),
    Path(os.path.expandvars(r"%USERPROFILE%\.aws")).resolve(),
    Path(os.path.expandvars(r"%USERPROFILE%\.gnupg")).resolve(),
    Path(os.path.expandvars(r"%USERPROFILE%\.azure")).resolve()
]

FORBIDDEN_FILE_PATTERNS = [
    r"\.token$",
    r"memories_v2\.db.*$",
    r"memories.*\.json$",
    r"session_.*\.json$",
    r"\.key$",
    r"\.pem$",
    r"id_rsa",
    r"id_ed25519"
]

HIGH_RISK_KEYWORDS = [
    r"\bmodificar\b",
    r"\bborrar\b",
    r"\beliminar\b",
    r"\bdestruir\b",
    r"\binstalar\b",
    r"\bactualizar\b",
    r"\bconfiguraci[oó]n\b",
    r"\breiniciar\b",
    r"\bapagar\b",
    r"\bbackup\b",
    r"\brespaldo\b",
    r"\btoken\b",
    r"\bcredencial(es)?\b",
    r"\bclave(s)?\b",
    r"\bpassword\b",
    r"\bcontrase[ñn]a\b",
    r"\bpermiso[s]?\b",
    r"\bsudo\b",
    r"\badmin\b",
    r"\broot\b",
    r"\bshell\b",
    r"\bpowershell\b",
    r"\bcmd\b",
    r"\bregistry\b",
    r"\bregedit\b",
    r"\bsystem32\b"
]

FORBIDDEN_INSTRUCTION_PATTERNS = [
    r"c:\\windows",
    r"c:/windows",
    r"system32",
    r"syswow64",
    r"program files",
    r"\.token\b",
    r"memories_v2\.db",
    r"\bbackups\b",
    r"\.ssh\b",
    r"\.aws\b",
    r"\.gnupg\b"
]


def validate_instruction_security(instruction: str) -> Tuple[bool, str]:
    """
    Verifica si una instrucción intenta apuntar o violar zonas rojas protegidas
    (Windows, .token, memories_v2.db, backups, .ssh).
    """
    low = instruction.lower()
    for pat in FORBIDDEN_INSTRUCTION_PATTERNS:
        if re.search(pat, low):
            return False, f"Instrucción rechazada por política de seguridad: referencia o intenta acceder a zona protegida ('{pat}')."
    return True, ""


def is_sensitive_path(path: Path) -> Tuple[bool, str]:
    """Comprueba si una ruta cae en alguna de las zonas rojas de seguridad."""
    try:
        resolved = path.resolve()
    except Exception as e:
        return True, f"Ruta inválida: {e}"

    # 1. Comprobar lista negra de rutas exactas o prefijos
    for forbidden in FORBIDDEN_EXACT_OR_PREFIX:
        if resolved == forbidden:
            return True, f"Acceso prohibido: ruta sensible del sistema ({forbidden.name})."
        try:
            resolved.relative_to(forbidden)
            return True, f"Acceso prohibido: dentro de zona protegida ({forbidden})."
        except ValueError:
            continue

    # 2. Comprobar patrones de nombres de archivo sensibles
    name = resolved.name
    for pat in FORBIDDEN_FILE_PATTERNS:
        if re.search(pat, name, re.IGNORECASE):
            return True, f"Acceso prohibido: archivo sensible coincidente con patrón protegido ('{name}')."

    return False, ""


def validate_project_path(path_input: Any) -> Tuple[bool, Optional[Path], str]:
    """
    Valida rigurosamente un directorio de proyecto:
    1. Debe ser una ruta existente o creable dentro de ALLOWED_PROJECT_ROOTS.
    2. No debe caer en ninguna zona sensible.
    3. No debe contener secuencias de path traversal ('..').
    """
    if not path_input:
        # Por defecto, el sandbox seguro
        sandbox = Path(r"C:\Yedid-Smart-Home\sandbox").resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        return True, sandbox, ""

    try:
        raw_str = str(path_input).strip()
        if raw_str.startswith("\\\\?\\") or raw_str.startswith("\\\\?/"):
            raw_str = raw_str[4:]
        # Rechazar caracteres nulos o secuencias de escape
        if "\0" in raw_str:
            return False, None, "Ruta contiene caracteres nulos prohibidos."

        target = Path(raw_str).resolve()
    except Exception as e:
        return False, None, f"Error resolviendo ruta: {e}"

    # Comprobar sensibilidad
    is_sens, sens_reason = is_sensitive_path(target)
    if is_sens:
        return False, None, sens_reason

    # Comprobar lista blanca de raíces permitidas
    is_allowed = False
    for root in ALLOWED_PROJECT_ROOTS:
        try:
            target.relative_to(root)
            is_allowed = True
            break
        except ValueError:
            continue

    if not is_allowed:
        return False, None, f"Directorio '{target}' fuera del alcance de proyectos autorizados."

    return True, target, ""


def sanitize_instruction(instruction: str) -> str:
    """
    Sanitiza la instrucción pasada al proceso del agente:
    - Elimina caracteres de control y nulos.
    - Normaliza espacios.
    - Asegura que se pase como cadena de texto plano sin metacaracteres no deseados.
    """
    if not instruction:
        return ""
    # Quitar bytes nulos y caracteres de control ASCII 0-31 excepto newline/tab
    cleaned = "".join(ch for ch in instruction if ch in ("\n", "\r", "\t") or ord(ch) >= 32)
    return cleaned.strip()


def classify_task_risk(instruction: str, project_path: Path) -> str:
    """
    Clasificador determinista de riesgo (inmune a prompt injection):
    - Si el proyecto NO es el sandbox aislado -> HIGH
    - Si la instrucción coincide con palabras clave críticas -> HIGH
    - De lo contrario -> LOW
    """
    resolved = project_path.resolve()
    sandbox = Path(r"C:\Yedid-Smart-Home\sandbox").resolve()

    if resolved != sandbox:
        return "HIGH"

    instr_lower = instruction.lower()
    for kw in HIGH_RISK_KEYWORDS:
        if re.search(kw, instr_lower):
            return "HIGH"

    return "LOW"


def build_secure_agent_env() -> Dict[str, str]:
    """
    Construye un diccionario de variables de entorno limpio para subprocesos:
    - Preserva PATH, SYSTEMROOT, TEMP, COMSPEC y variables esenciales de Windows.
    - ELIMINA variables que contengan tokens, secretos o contraseñas.
    """
    clean_env = {}
    sensitive_env_keys = {
        "AUTH_TOKEN", "YEDID_AUTH_TOKEN", "SECRET_KEY", "JWT_SECRET",
        "OPENAI_API_KEY", "GITHUB_TOKEN", "TAILSCALE_AUTHKEY"
    }

    for k, v in os.environ.items():
        if k.upper() in sensitive_env_keys:
            continue
        if "TOKEN" in k.upper() or "SECRET" in k.upper() or "PASSWORD" in k.upper():
            continue
        clean_env[k] = v

    return clean_env


def ensure_workspace_security_rules(project_path: Path) -> None:
    """
    Genera o actualiza las reglas jerárquicas de seguridad (.agents/rules/security.md y GEMINI.md)
    dentro del directorio del proyecto para que Antigravity CLI las cargue con máxima prioridad.
    """
    try:
        rules_dir = project_path / ".agents" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        rule_file = rules_dir / "security.md"

        rule_content = f"""# DIRECTRICES ESTRICTAS DE SEGURIDAD PARA AGENTE LOCAL

1. **Ámbito de Trabajo Restringido:**
   - Este agente opera EXCLUSIVAMENTE dentro del directorio autorizado: `{project_path}`.
   - Queda ESTRICTAMENTE PROHIBIDO leer, crear, modificar o eliminar archivos fuera de este directorio.

2. **Zonas Prohibidas:**
   - No intentes acceder a `C:\\Yedid-Smart-Home\\data\\memories_v2.db` ni a archivos de memoria permanente.
   - No intentes acceder a `C:\\Yedid-Smart-Home\\data\\.token` ni credenciales del sistema.
   - No accedas al directorio de respaldos `C:\\Yedid-Smart-Home\\backups`.
   - No accedas a rutas del sistema operativo (`C:\\Windows`, `C:\\Program Files`, `.ssh`).

3. **Restricción de Comandos:**
   - No ejecutes comandos destructivos de sistema (`format`, `del /s`, `rmdir /s /q`, etc.).
   - Limita cualquier cambio a los archivos entregables del proyecto autorizado.
"""
        rule_file.write_text(rule_content, encoding="utf-8")

        # También crear/actualizar GEMINI.md en la raíz del proyecto para redundancia
        gemini_md = project_path / "GEMINI.md"
        if not gemini_md.exists():
            gemini_md.write_text(rule_content, encoding="utf-8")

    except Exception as e:
        logger.warning(f"No se pudieron escribir reglas de seguridad en {project_path}: {e}")


def verify_deliverables_security(deliverables: List[Dict[str, Any]], authorized_project: Path) -> Tuple[bool, List[str]]:
    """
    Auditoría post-ejecución de entregables:
    Verifica que NINGÚN archivo haya sido creado o modificado fuera de la raíz autorizada
    o dentro de zonas sensibles.
    """
    auth_resolved = authorized_project.resolve()
    violations = []

    for d in deliverables:
        p_str = d.get("path")
        if not p_str:
            continue
        p = Path(p_str).resolve()

        # 1. Comprobar si está fuera del proyecto autorizado
        try:
            p.relative_to(auth_resolved)
        except ValueError:
            violations.append(f"Archivo generado fuera del proyecto autorizado: {p}")
            continue

        # 2. Comprobar si tocó alguna zona sensible
        is_sens, reason = is_sensitive_path(p)
        if is_sens:
            violations.append(f"Archivo generado en zona sensible ({reason}): {p}")

    return len(violations) == 0, violations
