# -*- coding: utf-8 -*-
"""
scripts/manage_bridge.py

Herramienta de administración CLI para Yedid Agent Bridge.
Uso:
  python scripts/manage_bridge.py --status
  python scripts/manage_bridge.py --set-token
  python scripts/manage_bridge.py --init-repo
  python scripts/manage_bridge.py --verify-repo
  python scripts/manage_bridge.py --poll-once
  python scripts/manage_bridge.py --pause
  python scripts/manage_bridge.py --resume
  python scripts/manage_bridge.py --test-ping
"""

import sys
import os
import getpass
import asyncio
import hashlib
from pathlib import Path

# Agregar raíz al sys.path
REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from agent_bridge.config import (
    load_bridge_config,
    save_bridge_config,
    get_github_token,
    save_github_token,
    is_bridge_paused,
    set_bridge_paused,
    TOKEN_FILE,
    LOCK_FILE
)
from agent_bridge.github_client import GitHubBridgeClient, GitHubSecurityException
from agent_bridge.task_store import TaskStore
from agent_bridge.dispatcher import BridgeDispatcher
from agent_bridge.schemas import AgentTask

def cmd_status():
    print("=" * 65)
    print(" ESTADO DE YEDID AGENT BRIDGE")
    print("=" * 65)
    cfg = load_bridge_config()
    token = get_github_token()

    print(f"• Repositorio objetivo:   {cfg.get('repo_owner') or '(No detectado)'}/{cfg.get('repo_name')}")
    print(f"• Intervalo de consulta:  {cfg.get('poll_interval_seconds')} segundos")
    print(f"• Operadores autorizados: {', '.join(cfg.get('authorized_users', []))}")
    print(f"• Estado de pausa:        {'⏸️ PAUSADO' if is_bridge_paused() else '▶️ ACTIVO'}")

    # Estado del Token
    if token:
        fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        print(f"• Token de GitHub:        Configurado (Huella SHA-256: {fp}...)")
        if TOKEN_FILE.exists():
            print(f"  Ruta segura:            {TOKEN_FILE}")
    else:
        print("• Token de GitHub:        ❌ NO CONFIGURADO")

    # Estado del Demonio / Proceso
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            import psutil
            if psutil.pid_exists(pid):
                print(f"• Demonio en ejecución:   ✅ ACTIVO (PID {pid})")
            else:
                print(f"• Demonio en ejecución:   ⚠️ Cerrojo huérfano (PID {pid} inactivo)")
        except Exception:
            print("• Demonio en ejecución:   ⚠️ Error leyendo lockfile")
    else:
        print("• Demonio en ejecución:   💤 DETENIDO")

    # Estadísticas del almacén
    store = TaskStore()
    stats = store.get_stats()
    print(f"• Tareas procesadas:      Total: {stats['total_tasks']} | Éxito: {stats['completed']} | Fallos: {stats['failed']}")
    print("=" * 65)

def cmd_set_token(token_arg=None):
    print("=" * 65)
    print(" CONFIGURACIÓN SEGURA DE TOKEN DE GITHUB")
    print("=" * 65)
    print("El token debe tener permisos mínimos de lectura y escritura de Issues.")
    print("Nunca será expuesto en logs ni subido a repositorios Git.")
    print("-" * 65)

    if token_arg:
        token = token_arg.strip()
    else:
        token = getpass.getpass("Ingresa o pega tu GitHub Personal Access Token (oculto): ").strip()

    if not token:
        print("❌ Operación cancelada. El token no puede estar vacío.")
        return 1

    # Validar token contra la API de GitHub
    print("Verificando credencial con la API de GitHub...")
    client = GitHubBridgeClient(token=token)
    try:
        user_info = asyncio.run(client.get_authenticated_user())
        username = user_info.get("login")
        print(f"✅ Autenticación exitosa como usuario de GitHub: @{username}")
    except Exception as e:
        print(f"❌ Error validando token con GitHub: {e}")
        return 1

    # Guardar en Windows Credential Manager y archivo protegido
    save_github_token(token, username=username)
    print("✅ Token resguardado en Windows Credential Manager (Target: YedidAgentBridge:github_token).")
    print(f"✅ Respaldo local de servicio protegido en {TOKEN_FILE} con permisos NTFS restringidos.")

    # Actualizar usuario en configuración
    cfg = load_bridge_config()
    cfg["repo_owner"] = username
    if username.lower() not in [u.lower() for u in cfg.get("authorized_users", [])]:
        cfg["authorized_users"].append(username.lower())
    save_bridge_config(cfg)
    print(f"✅ Propietario configurado como '{username}' y añadido a la lista blanca de operadores.")

    # Verificación inmediata del repositorio
    repo_name = cfg.get("repo_name", "yedid-agent-bridge")
    print(f"\nVerificando estado del repositorio '{username}/{repo_name}' en GitHub...")
    try:
        repo_data = asyncio.run(client.get_repo(username, repo_name))
        if repo_data:
            is_priv = repo_data.get("private") is True
            has_issues = repo_data.get("has_issues") is True
            print(f"• URL Exacta:       {repo_data.get('html_url')}")
            print(f"• Visibilidad:      {'🔒 PRIVADO (Verificado OK)' if is_priv else '❌ PÚBLICO (Inseguro)'}")
            print(f"• GitHub Issues:    {'✅ Habilitado' if has_issues else '❌ Deshabilitado'}")
            if not is_priv:
                print("⚠️ ATENCIÓN: El repositorio no es privado. Cámbialo a Private en GitHub Settings.")
            if not has_issues:
                print("⚠️ ATENCIÓN: GitHub Issues está deshabilitado. Actívalo en Settings -> General -> Features -> Issues.")
        else:
            print(f"ℹ️ El repositorio '{username}/{repo_name}' aún no existe. Puedes crearlo con: python scripts/manage_bridge.py --init-repo")
    except Exception as e:
        print(f"⚠️ Nota en verificación de repositorio: {e}")

    return 0

def cmd_init_repo():
    print("=" * 65)
    print(" INICIALIZACIÓN DE REPOSITORIO PRIVADO EN GITHUB")
    print("=" * 65)
    token = get_github_token()
    if not token:
        print("❌ Primero debes configurar el token con: python scripts/manage_bridge.py --set-token")
        return 1

    cfg = load_bridge_config()
    client = GitHubBridgeClient(token=token)
    repo_name = cfg.get("repo_name", "yedid")

    try:
        user_info = asyncio.run(client.get_authenticated_user())
        owner = user_info.get("login")
        print(f"Usuario autenticado: @{owner}")

        # Comprobar si ya existe
        existing = asyncio.run(client.get_repo(owner, repo_name))
        if existing:
            print(f"El repositorio '{owner}/{repo_name}' ya existe.")
            is_priv = existing.get("private") is True
            if is_priv:
                print("✅ Verificado: El repositorio es estrictamente PRIVADO.")
                return 0
            else:
                print("❌ PELIGRO CRÍTICO: El repositorio existe pero es PÚBLICO. Cámbialo a PRIVADO en GitHub.")
                return 1

        print(f"Creando nuevo repositorio privado '{owner}/{repo_name}'...")
        res = asyncio.run(client.create_private_repo(
            repo_name=repo_name,
            description="Repositorio privado de comunicación asíncrona para Yedid Agent Bridge."
        ))
        if res.get("private") is True:
            print(f"✅ Repositorio privado '{owner}/{repo_name}' creado con éxito en GitHub.")
            print(f"• URL: {res.get('html_url')}")
            print(f"• Visibilidad: {res.get('visibility', 'private').upper()}")
            print(f"• Issues Habilitados: {res.get('has_issues')}")
            return 0
        else:
            print("❌ Error: El repositorio no se creó como privado.")
            return 1

    except Exception as e:
        print(f"❌ Error inicializando repositorio: {e}")
        return 1

def cmd_verify_repo():
    print("=" * 65)
    print(" VERIFICACIÓN DE PRIVACIDAD DEL REPOSITORIO")
    print("=" * 65)
    token = get_github_token()
    if not token:
        print("❌ Token no configurado.")
        return 1

    cfg = load_bridge_config()
    client = GitHubBridgeClient(token=token)
    owner = cfg.get("repo_owner")
    repo = cfg.get("repo_name", "yedid")

    if not owner:
        user_info = asyncio.run(client.get_authenticated_user())
        owner = user_info.get("login")

    try:
        asyncio.run(client.verify_repo_is_private(owner, repo))
        print(f"✅ VERIFICADO: El repositorio '{owner}/{repo}' es 100% PRIVADO.")
        return 0
    except GitHubSecurityException as e:
        print(f"❌ VIOLACIÓN DE PRIVACIDAD: {e}")
        return 1
    except Exception as e:
        print(f"❌ Error comprobando privacidad: {e}")
        return 1

def cmd_poll_once():
    print("=" * 65)
    print(" EJECUCIÓN DE CICLO ÚNICO DE POLLING (DEBUG)")
    print("=" * 65)
    dispatcher = BridgeDispatcher()
    res = asyncio.run(dispatcher.poll_and_process_cycle())
    print(f"Resultado del ciclo: {res}")
    return 0 if res.get("status") in ("ok", "paused") else 1

def cmd_test_ping():
    print("=" * 65)
    print(" PRUEBA LOCAL DE AGENTE: TASK YEDID_BRIDGE_PING_001")
    print("=" * 65)
    task = AgentTask(
        schema="yedid-agent-task/1.0",
        task_id="YEDID_BRIDGE_PING_001",
        target_agent="ollama",
        operation="respond",
        priority="normal",
        instructions="Responde exactamente 'PONG YEDID'",
        requires_approval=False
    )
    print(f"Despachando tarea sintética a Ollama ({task.task_id})...")
    from agent_bridge.adapters.ollama_adapter import OllamaAdapter
    adapter = OllamaAdapter()
    res = asyncio.run(adapter.execute(task))
    print("\nRespuesta obtenida del Agente Ollama:")
    print(f"• Estado:    {res.status.upper()}")
    print(f"• Agente:    {res.agent}")
    print(f"• Resultado: \"{res.result}\"")
    print(f"• Metadatos: {res.metadata}")
    
    if "PONG YEDID" in res.result.upper():
        print("\n✅ PRUEBA DE ADAPTADOR OLLAMA SATISFACTORIA AL 100%.")
        return 0
    else:
        print("\n⚠️ La respuesta difiere de lo esperado.")
        return 1

def main():
    if len(sys.argv) < 2:
        cmd_status()
        print("\nComandos disponibles:")
        print("  --status        Muestra la configuración y estado del Bridge")
        print("  --set-token     Configura el token de GitHub de forma segura")
        print("  --init-repo     Crea o verifica el repositorio privado en GitHub")
        print("  --verify-repo   Verifica que el repositorio sea estrictamente privado")
        print("  --poll-once     Ejecuta un único ciclo de consulta y procesamiento")
        print("  --pause         Pausa temporalmente el polling del Bridge")
        print("  --resume        Reanuda el polling del Bridge")
        print("  --test-ping     Prueba local de la tarea YEDID_BRIDGE_PING_001 en Ollama")
        return 0

    arg = sys.argv[1].lower()
    if arg in ("--status", "-s"):
        return cmd_status()
    elif arg == "--set-token":
        tok = sys.argv[2] if len(sys.argv) > 2 else None
        return cmd_set_token(tok)
    elif arg == "--init-repo":
        return cmd_init_repo()
    elif arg == "--verify-repo":
        return cmd_verify_repo()
    elif arg == "--poll-once":
        return cmd_poll_once()
    elif arg == "--pause":
        set_bridge_paused(True)
        print("⏸️ Bridge pausado.")
        return 0
    elif arg == "--resume":
        set_bridge_paused(False)
        print("▶️ Bridge reanudado.")
        return 0
    elif arg == "--test-ping":
        return cmd_test_ping()
    else:
        print(f"Comando desconocido: {arg}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
