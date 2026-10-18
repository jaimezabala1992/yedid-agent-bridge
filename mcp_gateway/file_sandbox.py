"""
Sandboxing y operaciones seguras sobre archivos para Yedid MCP Gateway.
Restringe todas las operaciones de archivos exclusivamente a carpetas autorizadas de proyectos.
Protección contra:
1. Path traversal (.., symlinks, rutas absolutas externas).
2. Filtración o sobreescritura de credenciales, .env, claves privadas o tokens.
3. Creación de ejecutables o scripts maliciosos (.exe, .bat, .cmd, .ps1).
"""
import os
import tempfile
import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional
from hub.audit import log_event

# Directorios autorizados para gestión de proyectos (raíces canónicas)
AUTHORIZED_ROOTS = [
    Path(os.path.realpath("C:/Yedid-Smart-Home")),
    Path(os.path.realpath("%USERPROFILE%/Documents/Codex"))
]

# Subdirectorios autorizados estrictamente para ESCRITURA
# (PROHIBIDO escribir en el núcleo: hub/, mcp_gateway/, raíz de proyecto, scripts base, data y backups)
ALLOWED_WRITE_SUBDIRS = ["sandbox", "projects", "exports"]

# Directorios estrictamente confidenciales y PROHIBIDOS (lectura, escritura y listado)
# Incluye bases de datos, respaldos, claves y secretos
FORBIDDEN_DIR_PATTERNS = [
    "*backup*",
    "*data*",
    ".*tvremote*",
    ".*git*"
]

# Patrones de archivos estrictamente prohibidos para lectura, escritura o listado
FORBIDDEN_FILE_PATTERNS = [
    ".env*",
    "*.key",
    "*.pem",
    "*id_rsa*",
    "*token*",
    "*credential*",
    "*.pfx",
    "*.cer",
    "*.crt",
    "*.exe",
    "*.bat",
    "*.cmd",
    "*.ps1",
    "*.vbs",
    "*.dll",
    "*.db*",
    "*.sqlite*",
    "*memory*",
    "*seed*",
    "*backup*",
    "approvals.json",
    "audit.jsonl"
]

MAX_READ_BYTES = 65536  # 64 KB máximo por lectura
MAX_WRITE_BYTES = 65536  # 64 KB máximo por escritura

class FileSandbox:
    def __init__(self, allowed_roots: List[Path] = AUTHORIZED_ROOTS, allowed_write_subdirs: List[str] = ALLOWED_WRITE_SUBDIRS):
        self.allowed_roots = allowed_roots
        self.allowed_write_subdirs = allowed_write_subdirs

    def _resolve_and_validate_path(self, relative_path: str, root_idx: int = 0, for_write: bool = False) -> Path:
        """
        Resuelve una ruta relativa y valida que:
        1. No escape de las raíces autorizadas (is_relative_to sobre realpath canónico, previene prefix attacks).
        2. No atraviese enlaces simbólicos ni directory junctions que apunten fuera.
        3. No acceda a archivos sensibles ni patrones restringidos (.env, claves, ejecutables).
        4. Si es para escritura, esté confinado exclusivamente a subdirectorios permitidos (ALLOWED_WRITE_SUBDIRS).
        """
        clean_rel = relative_path.replace("\\", "/").strip().lstrip("/")
        if not clean_rel:
            clean_rel = "."

        # Seleccionar raíz autorizada (por defecto C:\Yedid-Smart-Home)
        base = self.allowed_roots[0]
        if root_idx < len(self.allowed_roots):
            base = self.allowed_roots[root_idx]
        real_base = Path(os.path.realpath(str(base)))

        # Resolver ruta objetivo
        raw_target = base / clean_rel
        resolved_target = raw_target.resolve()
        real_target = Path(os.path.realpath(str(resolved_target)))

        # 1. Comprobación estricta de pertenencia canónica (previene ataques de prefijos tipo C:\Yedid-Smart-Home-evil)
        try:
            is_inside = real_target.is_relative_to(real_base)
        except AttributeError:
            # Fallback para Python < 3.9
            is_inside = real_base in real_target.parents or real_target == real_base

        if not is_inside:
            log_event("sandbox_path_traversal_blocked", {
                "requested_path": relative_path,
                "resolved": str(resolved_target),
                "realpath": str(real_target)
            })
            raise PermissionError(f"Acceso denegado: La ruta '{relative_path}' escapa de los directorios de proyecto autorizados.")

        # 2. Detección y bloqueo de Enlaces Simbólicos y Junctions en la cadena de directorios
        check_node = resolved_target
        while check_node != base and check_node != check_node.parent:
            if check_node.is_symlink() or os.path.islink(str(check_node)):
                log_event("sandbox_symlink_blocked", {"path": relative_path, "symlink_node": str(check_node)})
                raise PermissionError(f"Acceso denegado: Se detectó un enlace simbólico o junction no autorizado en '{check_node.name}'.")
            check_node = check_node.parent

        # 3. Comprobar que ninguna parte de la ruta (incluyendo rutas indirectas) pertenezca a directorios prohibidos
        try:
            rel_parts = [p.lower() for p in real_target.relative_to(real_base).parts]
        except ValueError:
            rel_parts = []

        for part in rel_parts:
            for d_pat in FORBIDDEN_DIR_PATTERNS:
                if fnmatch.fnmatch(part, d_pat.lower()):
                    log_event("sandbox_forbidden_dir_blocked", {
                        "requested_path": relative_path,
                        "matched_part": part,
                        "pattern": d_pat
                    })
                    raise PermissionError(f"Acceso denegado: El directorio '{part}' es privado y está excluido de las herramientas MCP.")

        # 4. Comprobar archivos sensibles o patrones prohibidos
        filename = resolved_target.name.lower()
        if filename and filename != ".":
            for pat in FORBIDDEN_FILE_PATTERNS:
                if fnmatch.fnmatch(filename, pat.lower()):
                    log_event("sandbox_forbidden_file_blocked", {"requested_file": filename, "pattern": pat})
                    raise PermissionError(f"Acceso prohibido por política de seguridad al archivo '{filename}'.")

        # 5. Si es operación de ESCRITURA, restringir a carpetas permitidas
        if for_write:
            try:
                rel_to_base = real_target.relative_to(real_base)
            except ValueError:
                raise PermissionError(f"Escritura denegada: La ruta '{relative_path}' no pertenece a la base autorizada.")

            top_subdir = rel_to_base.parts[0].lower() if rel_to_base.parts else ""
            if top_subdir not in self.allowed_write_subdirs:
                log_event("sandbox_write_outside_allowed_subdirs_blocked", {
                    "requested_path": relative_path,
                    "attempted_subdir": top_subdir,
                    "allowed_subdirs": self.allowed_write_subdirs
                })
                raise PermissionError(
                    f"Escritura denegada en '{relative_path}'. "
                    f"Por seguridad, sólo se autoriza escribir dentro de: {self.allowed_write_subdirs}. "
                    f"El código núcleo del Hub, MCP, respaldos y almacenamiento de datos no pueden ser modificados."
                )

        return resolved_target

    def list_directory(self, relative_path: str = "", root_idx: int = 0) -> Dict[str, Any]:
        """Lista el contenido de un directorio dentro del sandbox de proyecto."""
        target_dir = self._resolve_and_validate_path(relative_path, root_idx, for_write=False)
        if not target_dir.exists():
            return {"success": False, "error": f"El directorio '{relative_path}' no existe."}
        if not target_dir.is_dir():
            return {"success": False, "error": f"La ruta '{relative_path}' no es un directorio."}

        entries = []
        for item in sorted(target_dir.iterdir()):
            fname = item.name.lower()
            if any(fnmatch.fnmatch(fname, pat.lower()) for pat in FORBIDDEN_FILE_PATTERNS):
                continue
            if any(fnmatch.fnmatch(fname, pat.lower()) for pat in FORBIDDEN_DIR_PATTERNS):
                continue
            if item.name.startswith((".git", "__pycache__")):
                continue

            entries.append({
                "name": item.name,
                "is_dir": item.is_dir(),
                "size_bytes": item.stat().st_size if item.is_file() else None
            })

        log_event("sandbox_list_dir", {"path": relative_path, "count": len(entries)})
        return {
            "success": True,
            "directory": relative_path or "/",
            "total_items": len(entries),
            "entries": entries[:50]
        }

    def read_file(self, relative_path: str, max_lines: int = 200, root_idx: int = 0) -> Dict[str, Any]:
        """Lee el contenido de un archivo autorizado de texto dentro del sandbox."""
        target_file = self._resolve_and_validate_path(relative_path, root_idx, for_write=False)
        if not target_file.exists():
            return {"success": False, "error": f"El archivo '{relative_path}' no existe."}
        if not target_file.is_file():
            return {"success": False, "error": f"La ruta '{relative_path}' no es un archivo de datos."}

        size = target_file.stat().st_size
        if size > MAX_READ_BYTES:
            return {"success": False, "error": f"El archivo excede el tamaño máximo permitido para lectura ({round(size/1024, 1)} KB > 64 KB)."}

        try:
            content = target_file.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            truncated = len(lines) > max_lines
            display_lines = lines[:max_lines]

            log_event("sandbox_file_read", {"file": relative_path, "bytes": size})
            return {
                "success": True,
                "file": relative_path,
                "lines_count": len(lines),
                "truncated": truncated,
                "content": "\n".join(display_lines)
            }
        except Exception as e:
            return {"success": False, "error": f"Error al leer archivo: {e}"}

    def write_file(self, relative_path: str, content: str, allow_overwrite: bool = False, root_idx: int = 0) -> Dict[str, Any]:
        """Escribe un archivo de forma atómica dentro de las carpetas autorizadas del sandbox."""
        target_file = self._resolve_and_validate_path(relative_path, root_idx, for_write=True)
        
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_WRITE_BYTES:
            return {"success": False, "error": f"El contenido a escribir excede el límite de 64 KB ({content_bytes} bytes)."}

        exists = target_file.exists()
        if exists and not allow_overwrite:
            return {
                "success": False,
                "requires_overwrite_confirmation": True,
                "error": f"El archivo '{relative_path}' ya existe. Sobreescribirlo es una acción de ALTO RIESGO que requiere autorización humana."
            }

        # Asegurar directorio padre
        target_file.parent.mkdir(parents=True, exist_ok=True)

        # Escritura Atómica para evitar condiciones de carrera (TOCTOU)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target_file.parent), prefix=".tmp_yedid_")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(target_file))
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            raise e

        log_event("sandbox_file_written", {
            "file": relative_path,
            "bytes": content_bytes,
            "overwritten": exists
        })

        return {
            "success": True,
            "file": relative_path,
            "bytes_written": content_bytes,
            "overwritten": exists,
            "message": f"Archivo '{relative_path}' guardado de forma atómica y segura en el sandbox de proyecto."
        }

file_sandbox = FileSandbox()
