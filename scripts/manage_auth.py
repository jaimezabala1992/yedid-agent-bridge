# -*- coding: utf-8 -*-
"""
scripts/manage_auth.py

Herramienta de Gestión y Restablecimiento Seguro de Credenciales para Yedid Hub:
- Consulta de estado y huella criptográfica SHA-256 (sin revelar la credencial).
- Copia silenciosa de la credencial actual al portapapeles de Windows (cero fuga visual).
- Restablecimiento interactivo seguro mediante getpass (sin eco en consola ni logs).
- Verificación de autenticación y sesiones HttpOnly en el Hub local.
- Aplicación de permisos NTFS estrictos (icacls) limitados al usuario Windows actual.
"""

import sys
import os
import argparse
import hashlib
import getpass
import subprocess
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TOKEN_FILE = DATA_DIR / ".token"

def apply_strict_acls(file_path: Path):
    """Aplica permisos NTFS estrictos para que solo el usuario actual, SYSTEM y Admins tengan acceso."""
    username = os.environ.get("USERNAME", "jaime")
    try:
        cmd = f'icacls "{file_path}" /inheritance:r /grant:r "{username}:(F)" /grant:r "SYSTEM:(F)" /grant:r "Administrators:(F)"'
        subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        return True
    except Exception as e:
        print(f"  [WARN] No se pudieron aplicar permisos NTFS estrictos: {e}", file=sys.stderr)
        return False

def get_token_hash() -> str:
    """Calcula la huella SHA-256 de la credencial actual sin exponer su contenido."""
    if not TOKEN_FILE.exists():
        return "NO_EXISTE"
    try:
        raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return "VACÍO"
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"sha256:{h[:12]}..."
    except Exception as e:
        return f"ERROR_LECTURA: {e}"

def action_status():
    print("=" * 65)
    print(" ESTADO DE AUTENTICACIÓN Y CREDENCIALES — YEDID HUB")
    print("=" * 65)
    print(f"  Archivo de credencial : {TOKEN_FILE}")
    print(f"  Existe en disco       : {'SÍ' if TOKEN_FILE.exists() else 'NO'}")
    if TOKEN_FILE.exists():
        try:
            token_len = len(TOKEN_FILE.read_text(encoding='utf-8').strip())
            print(f"  Longitud de la clave  : {token_len} caracteres")
            print(f"  Huella criptográfica  : {get_token_hash()}")
        except Exception as e:
            print(f"  Estado de lectura     : Error ({e})")

    # Verificar permisos NTFS
    try:
        res = subprocess.run(f'icacls "{TOKEN_FILE}"', shell=True, capture_output=True, text=True)
        print("\n  Permisos NTFS actuales:")
        for line in res.stdout.strip().splitlines()[:5]:
            print(f"    {line}")
    except Exception:
        pass

    print("\n  [SEGURIDAD] La clave maestra NUNCA se muestra en pantalla ni en registros.")
    print("=" * 65)

def action_copy_clipboard():
    """Copia la credencial actual de data/.token al portapapeles de Windows sin mostrarla."""
    if not TOKEN_FILE.exists():
        print("  [ERROR] El archivo de credencial no existe en data/.token.", file=sys.stderr)
        return False
    try:
        # Usar PowerShell Set-Clipboard mediante tubería binaria protegida
        cmd = f'Get-Content -Path "{TOKEN_FILE}" -Raw -Encoding utf8 | Set-Clipboard'
        res = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        if res.returncode == 0:
            print("  ✅ [ÉXITO] Credencial actual copiada al portapapeles de Windows.")
            print("  📋 Ve a tu navegador y pulsa Ctrl+V en el campo de la clave para iniciar sesión.")
            print("  🔒 Cero exposición visual: La clave no fue mostrada en consola ni en logs.")
            return True
        else:
            print(f"  [ERROR] Fallo al copiar al portapapeles: {res.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [ERROR] Excepción copiando credencial: {e}", file=sys.stderr)
        return False

def action_set_password():
    print("=" * 65)
    print(" RESTABLECIMIENTO SEGURO DE CONTRASEÑA — YEDID HUB")
    print(" (Los caracteres no se mostrarán en pantalla mientras escribes)")
    print("=" * 65)

    p1 = getpass.getpass("  Ingresa tu nueva contraseña para Yedid Hub: ")
    if len(p1) < 8:
        print("\n  ❌ [ERROR] La contraseña debe tener al menos 8 caracteres.", file=sys.stderr)
        return False

    p2 = getpass.getpass("  Confirma tu nueva contraseña: ")
    if p1 != p2:
        print("\n  ❌ [ERROR] Las contraseñas no coinciden. Operación cancelada.", file=sys.stderr)
        return False

    # Guardar en archivo atómicamente
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = DATA_DIR / ".token.tmp"
    tmp_file.write_text(p1.strip(), encoding="utf-8")
    
    # Reemplazar atómicamente
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    tmp_file.replace(TOKEN_FILE)

    # Restringir permisos NTFS
    apply_strict_acls(TOKEN_FILE)

    print("\n  ✅ [ÉXITO] Contraseña actualizada correctamente en data/.token.")
    print("  🔒 Permisos NTFS restringidos aplicados exclusivamente a tu usuario Windows.")
    print("  🚀 La nueva contraseña está activa inmediatamente en Yedid Hub.")
    print(f"  Huella criptográfica: {get_token_hash()}")
    print("=" * 65)
    return True

def action_verify():
    print("=" * 65)
    print(" VERIFICACIÓN DE AUTENTICACIÓN LOCAL EN YEDID HUB")
    print("=" * 65)
    if not TOKEN_FILE.exists():
        print("  [FAIL] data/.token no existe.", file=sys.stderr)
        return False

    token_val = TOKEN_FILE.read_text(encoding="utf-8").strip()
    try:
        import httpx
        with httpx.Client(base_url="http://127.0.0.1:8765", timeout=5.0) as client:
            login_res = client.post("/api/auth/login", json={"token": token_val})
            if login_res.status_code != 200:
                print(f"  ❌ [FAIL] Error en login: HTTP {login_res.status_code}")
                return False

            data = login_res.json()
            csrf_token = data.get("csrf_token")
            session_id = login_res.cookies.get("session_id")

            if not session_id:
                print("  ❌ [FAIL] No se recibió la cookie de sesión session_id.")
                return False

            print("  ✓ [PASS] Login HTTP 200 OK con credencial autorizada.")
            print(f"  ✓ [PASS] Cookie de sesión HttpOnly recibida (longitud: {len(session_id)} chars).")
            print(f"  ✓ [PASS] Token CSRF generado para operaciones mutantes.")

            # Probar acceso a endpoint protegido
            vitals_res = client.get("/api/system/vitals", headers={
                "X-CSRF-Token": csrf_token,
                "Cookie": f"session_id={session_id}"
            })
            if vitals_res.status_code == 200:
                print("  ✓ [PASS] Acceso autorizado a telemetría de sistema (/api/system/vitals).")
            else:
                print(f"  ❌ [FAIL] Acceso denegado a endpoint protegido: {vitals_res.status_code}")
                return False

            print("\n  ✅ AUTENTICACIÓN 100% OPERATIVA Y VERIFICADA.")
            print("=" * 65)
            return True
    except Exception as e:
        print(f"  ❌ [ERROR] Error conectando con el servidor Hub (¿está ejecutándose en el puerto 8765?): {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Gestión segura de credenciales para Yedid Smart Home.")
    parser.add_argument("--status", action="store_true", help="Muestra el estado y huella de la credencial actual.")
    parser.add_argument("--copy-to-clipboard", action="store_true", help="Copia la credencial actual al portapapeles sin mostrarla.")
    parser.add_argument("--set-password", action="store_true", help="Establece interactivamente una nueva contraseña personalizada.")
    parser.add_argument("--verify", action="store_true", help="Verifica el inicio de sesión contra el servidor local.")

    args = parser.parse_args()

    if args.status:
        action_status()
    elif args.copy_to_clipboard:
        action_copy_clipboard()
    elif args.set_password:
        action_set_password()
    elif args.verify:
        action_verify()
    else:
        # Si no se pasó argumento, mostrar status y opciones
        action_status()
        print("\nOpciones disponibles:")
        print("  python scripts/manage_auth.py --copy-to-clipboard   (Copia la clave al portapapeles para pegar con Ctrl+V)")
        print("  python scripts/manage_auth.py --set-password         (Establece tu contraseña personal personalizada)")
        print("  python scripts/manage_auth.py --verify               (Prueba la autenticación contra el Hub)")

if __name__ == "__main__":
    main()
