"""
scripts/github_sync.py

Utilidad de Auditoría de Seguridad Pre-Push y Sincronización con GitHub para Yedid Smart Hub:
- Verifica que ningún archivo en el repositorio contenga credenciales, tokens, correos personales,
  dominios privados de Tailscale, IPs LAN personales, rutas de usuario local ni bases de datos SQLite.
- Valida la sintaxis AST de todos los módulos Python antes de permitir un commit o push.
- Soporta inicialización Git local (Dulwich) y publicación segura hacia:
  https://github.com/jaimezabala1992/yedid-agent-bridge (rama main).
"""

import argparse
import ast
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_URL = "https://github.com/jaimezabala1992/yedid-agent-bridge.git"
GITHUB_OWNER = "jaimezabala1992"
GITHUB_REPO = "yedid-agent-bridge"
DEFAULT_BRANCH = "main"

FORBIDDEN_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".pem", ".key", ".crt", ".pfx", ".jsonl", ".log", ".bak"}
FORBIDDEN_FILENAMES = {".env", ".token", ".github_token"}

FORBIDDEN_CONTENT_PATTERNS = {
    "personal_email": re.compile(r"[a-zA-Z0-9_.+-]+@(?:yahoo|gmail|hotmail|outlook)\.com", re.IGNORECASE),
    "private_tailscale_node": re.compile(
        r"(?<![a-zA-Z0-9-])(?!yedid-hub\.tailnet\.ts\.net\b)(?!your-node\.tailnet\.ts\.net\b)[a-zA-Z0-9-]+\.[a-zA-Z0-9-]+\.ts\.net\b",
        re.IGNORECASE,
    ),
    "private_lan_ip": re.compile(r"\b10\.0\.0\.\d{1,3}\b"),
    "hardcoded_user_path": re.compile(r"C:(?:\\\\|\\|/)Users(?:\\\\|\\|/)(?!Public\b)[a-zA-Z0-9_-]+", re.IGNORECASE),
    "github_pat_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def collect_repo_files(root: Path = REPO_ROOT) -> List[Path]:
    """Devuelve todos los archivos del repositorio excluyendo .git y __pycache__."""
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if ".git" in rel_parts or "__pycache__" in rel_parts or ".pytest_cache" in rel_parts:
            continue
        files.append(p)
    return files


def run_security_audit(root: Path = REPO_ROOT) -> Tuple[bool, List[str], List[Dict[str, str]]]:
    """
    Audita el 100% de los archivos preparados en el repositorio:
    1. Comprueba que no existan extensiones ni nombres prohibidos (.db, .pem, .token, .env).
    2. Escanea el contenido de texto en busca de patrones sensibles o personales.
    3. Verifica que todos los archivos .py compilen sin errores de sintaxis (ast.parse).
    """
    violations: List[str] = []
    manifest: List[Dict[str, str]] = []

    files = collect_repo_files(root)
    for f in files:
        rel_str = str(f.relative_to(root)).replace("\\", "/")
        size = f.stat().st_size
        manifest.append({"path": rel_str, "size_bytes": str(size)})

        if f.name in FORBIDDEN_FILENAMES:
            violations.append(f"[ARCHIVO PROHIBIDO] {rel_str}")
        if f.suffix.lower() in FORBIDDEN_EXTENSIONS:
            violations.append(f"[EXTENSIÓN PROHIBIDA] {rel_str} ({f.suffix})")

        if f.suffix.lower() not in (".png", ".ico", ".jpg", ".webp"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            for rule_name, regex in FORBIDDEN_CONTENT_PATTERNS.items():
                match = regex.search(txt)
                if match:
                    violations.append(f"[DATO SENSIBLE: {rule_name}] en {rel_str} -> '{match.group(0)}'")

            if f.suffix.lower() == ".py":
                try:
                    ast.parse(txt, filename=rel_str)
                except SyntaxError as e:
                    violations.append(f"[ERROR SINTAXIS PYTHON] en {rel_str}: {e}")

    return (len(violations) == 0), violations, manifest


def init_and_commit_local_git(root: Path = REPO_ROOT, message: str = "chore: initial sanitized Yedid Smart Hub v2.0 repository for Gemini & ChatGPT collaboration") -> str:
    """Inicializa el repositorio Git local en la rama 'main', configura 'origin' y crea el commit."""
    from dulwich.repo import Repo
    from dulwich import porcelain

    git_dir = root / ".git"
    if not git_dir.exists():
        repo = Repo.init(str(root), default_branch=b"main")
    else:
        repo = Repo(str(root))

    config = repo.get_config()
    config.set((b"remote", b"origin"), b"url", REMOTE_URL.encode("utf-8"))
    config.set((b"remote", b"origin"), b"fetch", b"+refs/heads/*:refs/remotes/origin/*")
    config.set((b"user",), b"name", b"Jaime Zabala (Yedid Smart Hub)")
    config.set((b"user",), b"email", b"jaimezabala1992@users.noreply.github.com")
    config.write_to_path()

    rel_paths = [str(f.relative_to(root)) for f in collect_repo_files(root)]
    porcelain.add(repo, paths=[str(root / rp) for rp in rel_paths])

    commit_bytes = porcelain.commit(
        repo,
        message=message.encode("utf-8"),
        author=b"Jaime Zabala <jaimezabala1992@users.noreply.github.com>",
        committer=b"Gemini Antigravity <gemini-antigravity@users.noreply.github.com>",
    )
    commit_sha = commit_bytes.decode("ascii")
    # Asegurar que refs/heads/main y HEAD apunten a main
    repo.refs[b"refs/heads/main"] = commit_bytes
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    repo.close()
    return commit_sha


def push_to_github_api(root: Path = REPO_ROOT, commit_message: str = "Sync sanitized Yedid Smart Hub v2.0") -> Dict[str, str]:
    """
    Publica el árbol auditado hacia https://github.com/jaimezabala1992/yedid-agent-bridge (rama main)
    utilizando el token de GitHub almacenado en Windows Credential Manager o GITHUB_TOKEN.
    """
    import httpx
    prod_root = Path(r"C:\Yedid-Smart-Home")
    if prod_root.exists():
        sys.path.insert(0, str(prod_root))
    else:
        sys.path.insert(0, str(root))
    from agent_bridge.config import get_github_token

    ok, violations, manifest = run_security_audit(root)
    if not ok:
        raise RuntimeError(f"Push abortado por violaciones de seguridad:\n" + "\n".join(violations))

    token = get_github_token()
    if not token:
        raise RuntimeError(
            "No se encontró token de GitHub en Windows Credential Manager ('YedidAgentBridge:github_token') "
            "ni en la variable de entorno GITHUB_TOKEN."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Yedid-GitHub-Sync/2.0",
    }
    api_base = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"

    with httpx.Client(timeout=45.0, headers=headers) as client:
        # Verificar si el repositorio está completamente vacío (size == 0)
        repo_res = client.get(api_base)
        repo_res.raise_for_status()
        repo_info = repo_res.json()

        # Si el repositorio está vacío, la API Git Data requiere inicializar el primer archivo vía Contents API
        ref_res = client.get(f"{api_base}/git/ref/heads/{DEFAULT_BRANCH}")
        if ref_res.status_code in (404, 409):
            readme_bytes = (root / "README.md").read_bytes()
            init_res = client.put(
                f"{api_base}/contents/README.md",
                json={
                    "message": "chore: initialize main branch",
                    "content": base64.b64encode(readme_bytes).decode("ascii"),
                    "branch": DEFAULT_BRANCH,
                },
            )
            init_res.raise_for_status()
            ref_res = client.get(f"{api_base}/git/ref/heads/{DEFAULT_BRANCH}")

        ref_res.raise_for_status()
        parent_sha = ref_res.json()["object"]["sha"]

        # Crear blobs y entradas del árbol Git para todos los archivos auditados
        tree_entries = []
        for f in collect_repo_files(root):
            rel_posix = str(f.relative_to(root)).replace("\\", "/")
            raw_bytes = f.read_bytes()
            blob_res = client.post(
                f"{api_base}/git/blobs",
                json={
                    "content": base64.b64encode(raw_bytes).decode("ascii"),
                    "encoding": "base64",
                },
            )
            blob_res.raise_for_status()
            blob_sha = blob_res.json()["sha"]
            tree_entries.append({
                "path": rel_posix,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })

        tree_res = client.post(f"{api_base}/git/trees", json={"tree": tree_entries})
        tree_res.raise_for_status()
        tree_sha = tree_res.json()["sha"]

        commit_res = client.post(
            f"{api_base}/git/commits",
            json={
                "message": commit_message,
                "tree": tree_sha,
                "parents": [parent_sha],
            },
        )
        commit_res.raise_for_status()
        new_commit_sha = commit_res.json()["sha"]

        update_ref_res = client.patch(
            f"{api_base}/git/refs/heads/{DEFAULT_BRANCH}",
            json={"sha": new_commit_sha, "force": False},
        )
        update_ref_res.raise_for_status()

        return {
            "status": "pushed",
            "repository": f"{GITHUB_OWNER}/{GITHUB_REPO}",
            "branch": DEFAULT_BRANCH,
            "commit_sha": new_commit_sha,
            "files_published": str(len(tree_entries)),
        }


def main():
    parser = argparse.ArgumentParser(description="Auditoría y Sincronización Segura con GitHub para Yedid Smart Hub")
    parser.add_argument("--audit", action="store_true", help="Ejecuta la auditoría de seguridad y sintaxis sobre todos los archivos")
    parser.add_argument("--init-commit", action="store_true", help="Inicializa el repositorio Git local en la rama main y crea el commit inicial")
    parser.add_argument("--set-token", metavar="TOKEN", help="Guarda el Personal Access Token de GitHub en Windows Credential Manager")
    parser.add_argument("--push", action="store_true", help="Publica los archivos auditados hacia GitHub (requiere autorización)")
    parser.add_argument("-m", "--message", default="feat: Yedid Smart Hub v2.0 sanitized repository for Gemini & ChatGPT collaboration", help="Mensaje del commit")
    args = parser.parse_args()

    if args.set_token:
        sys.path.insert(0, r"C:\Yedid-Smart-Home")
        from agent_bridge.config import save_github_token
        save_github_token(args.set_token.strip())
        print("  ✅ Token de GitHub guardado de forma segura en Windows Credential Manager.")

    ok, violations, manifest = run_security_audit(REPO_ROOT)
    print("=" * 72)
    print(f"AUDITORÍA DE SEGURIDAD PRE-PUSH — {REPO_ROOT}")
    print("=" * 72)
    print(f"Total de archivos preparados: {len(manifest)}")
    print(f"Violaciones de seguridad / sintaxis detectadas: {len(violations)}")
    if violations:
        for v in violations:
            print("  ❌", v)
        sys.exit(1)
    else:
        print("  ✅ 0 secretos, 0 bases de datos SQLite, 0 certificados, 0 correos/IPs privadas, 100% AST Python válido.")

    if args.init_commit:
        sha = init_and_commit_local_git(REPO_ROOT, message=args.message)
        print(f"  ✅ Repositorio Git inicializado en rama '{DEFAULT_BRANCH}' (Remote: {REMOTE_URL})")
        print(f"  ✅ Commit local creado: {sha}")

    if args.push:
        res = push_to_github_api(REPO_ROOT, commit_message=args.message)
        print(f"  🚀 Push completado exitosamente: {json.dumps(res, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
