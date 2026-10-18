"""
Servidor oficial Yedid MCP Gateway usando el SDK oficial FastMCP (compatible con Streamable HTTP y SSE).
Expone 5 herramientas de lectura protegidas y 6 herramientas de acción autorizadas con clasificación de riesgo.
"""
import sys
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE_DIR = Path("C:/Yedid-Smart-Home")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from .security import authenticate_mcp_request
from .tools import (
    handle_get_hub_health,
    handle_get_pc_status,
    handle_get_sunshine_status,
    handle_get_tv_status,
    handle_get_recent_actions,
    handle_execute_tv_control,
    handle_execute_scene,
    handle_manage_gaming_apps,
    handle_manage_agent_tasks,
    handle_manage_project_files,
    handle_run_approved_script
)

# 1. Instanciación del servidor FastMCP
mcp = FastMCP(
    name="Yedid MCP Gateway",
    instructions=(
        "Servidor MCP privado y seguro para consultar y controlar el entorno doméstico de Yedid Smart Home "
        "(telemetría de PC, Sunshine, Google TV, escenas, tareas multiagente, archivos de proyectos y scripts autorizados). "
        "Incluye control de riesgo estricto (Low / Medium / High) y flujo de aprobación humana para operaciones sensibles."
    ),
    host="127.0.0.1",
    port=8766
)

# Configurar dominios permitidos para protección contra DNS Rebinding
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "yedid-hub.tailnet.ts.net:*"
    ],
    allowed_origins=[
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "https://yedid-hub.tailnet.ts.net:*"
    ]
)

# 2. Registro de las 5 herramientas de lectura oficiales (Read-Only)

@mcp.tool(
    name="get_hub_health",
    description="Consulta la salud operativa y subsistemas activos de Yedid Hub (FastAPI, Ollama local, Whisper y Tailscale)."
)
async def get_hub_health() -> Dict[str, Any]:
    return await handle_get_hub_health()

@mcp.tool(
    name="get_pc_status",
    description="Consulta el estado de hardware de la computadora principal de Jaime (uso de CPU %, memoria RAM libre y almacenamiento de discos locales C y D)."
)
async def get_pc_status() -> Dict[str, Any]:
    return await handle_get_pc_status()

@mcp.tool(
    name="get_sunshine_status",
    description="Consulta el estado del servidor de streaming de juegos Sunshine y la dedicación exclusiva de la GPU Radeon RX 6700 XT para gaming/Moonlight."
)
async def get_sunshine_status() -> Dict[str, Any]:
    return await handle_get_sunshine_status()

@mcp.tool(
    name="get_tv_status",
    description="Consulta el estado de encendido y la app actualmente abierta en el televisor ONN Google TV de la sala."
)
async def get_tv_status() -> Dict[str, Any]:
    return await handle_get_tv_status()

@mcp.tool(
    name="get_recent_actions",
    description="Consulta el historial de las últimas acciones y comandos ejecutados en el hogar (encendido de TV, teclas o escenas), con anonimización estricta de credenciales."
)
async def get_recent_actions(limit: int = 5) -> Dict[str, Any]:
    return await handle_get_recent_actions(limit=limit)

# 3. Registro de las 6 herramientas de acción autorizadas (Action Tools con Control de Riesgo)

@mcp.tool(
    name="execute_tv_control",
    description=(
        "Control directo del televisor ONN Google TV. "
        "Acciones: 'power_on', 'power_off', 'send_key' (teclas: UP, DOWN, LEFT, RIGHT, OK, BACK, HOME, VOLUME_UP, VOLUME_DOWN, MUTE, PLAY_PAUSE), "
        "y 'launch_app' (apps: youtube, netflix, spotify, prime, disney, tailscale)."
    )
)
async def execute_tv_control(
    action: str,
    key: Optional[str] = None,
    app: Optional[str] = None
) -> Dict[str, Any]:
    return await handle_execute_tv_control(action=action, key=key, app=app)

@mcp.tool(
    name="execute_scene",
    description=(
        "Ejecuta una escena domótica preconfigurada en Yedid Hub. "
        "Escenas autorizadas: 'modo_cine' (TV + Netflix), 'modo_gaming' (Sunshine + TV), 'modo_musica' (TV + Spotify), "
        "y 'modo_noche' (apagar TV y reposo)."
    )
)
async def execute_scene(scene_name: str) -> Dict[str, Any]:
    return await handle_execute_scene(scene_name=scene_name)

@mcp.tool(
    name="manage_gaming_apps",
    description=(
        "Administración controlada de Sunshine Game Streaming y aplicaciones de juego autorizadas. "
        "Aplicaciones en lista blanca: 'sunshine' (status, start, restart), 'steam' (status, start), 'moonlight' (status, start)."
    )
)
async def manage_gaming_apps(app_name: str, action: str) -> Dict[str, Any]:
    return await handle_manage_gaming_apps(app_name=app_name, action=action)

@mcp.tool(
    name="manage_agent_tasks",
    description=(
        "Consulta, creación y seguimiento de tareas para agentes locales (Ollama Qwen3, Whisper, Gemini CLI, Codex, ChatGPT Yedid). "
        "Acciones: 'list' (ver tareas recientes), 'create' (crear tarea con title y target_agent), "
        "'get' (consultar estado por task_id), 'update_status' (actualizar estado con task_id y new_status)."
    )
)
async def manage_agent_tasks(
    action: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    target_agent: Optional[str] = None,
    task_id: Optional[str] = None,
    new_status: Optional[str] = None
) -> Dict[str, Any]:
    return await handle_manage_agent_tasks(
        action=action,
        title=title,
        description=description,
        target_agent=target_agent,
        task_id=task_id,
        new_status=new_status
    )

@mcp.tool(
    name="manage_project_files",
    description=(
        "Operaciones seguras de lectura y escritura limitadas estrictamente a directorios de proyectos autorizados "
        "(C:\\Yedid-Smart-Home, %USERPROFILE%\\Documents\\Codex). "
        "Operaciones: 'list_dir' (listar carpeta), 'read_file' (leer texto, máx 64KB), 'write_file' (guardar texto). "
        "NOTA: Sobreescribir un archivo existente requiere confirmación humana (approval_id)."
    )
)
async def manage_project_files(
    operation: str,
    relative_path: str,
    content: Optional[str] = None,
    allow_overwrite: bool = False,
    approval_id: Optional[str] = None
) -> Dict[str, Any]:
    return await handle_manage_project_files(
        operation=operation,
        relative_path=relative_path,
        content=content,
        allow_overwrite=allow_overwrite,
        approval_id=approval_id
    )

@mcp.tool(
    name="run_approved_script",
    description=(
        "Ejecución de scripts previamente registrados y aprobados para diagnóstico y mantenimiento de la computadora y el hogar. "
        "Scripts permitidos: 'system_health_check' (diagnóstico), 'backup_configs' (respaldo instantáneo), "
        "'clean_temp_logs' (limpieza de temporales), 'windows_maintenance' (vistas de SO), "
        "'pc_hibernate' (hibernar PC - requiere aprobación humana previa)."
    )
)
async def run_approved_script(
    script_name: str,
    approval_id: Optional[str] = None
) -> Dict[str, Any]:
    return await handle_run_approved_script(
        script_name=script_name,
        approval_id=approval_id
    )

# 4. Middleware de autenticación y protección de transporte
class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Permitir endpoint de salud sin autenticación
        if request.url.path in ("/healthz", "/"):
            return await call_next(request)

        client_host = request.client.host if request.client else "unknown"
        headers_dict = {k.lower(): v for k, v in request.headers.items()}

        if not authenticate_mcp_request(headers_dict, client_host=client_host):
            return JSONResponse(
                {
                    "error": "Acceso no autorizado a Yedid MCP Gateway",
                    "detail": "Se requiere Bearer token válido o conexión desde localhost autorizada."
                },
                status_code=401
            )

        return await call_next(request)

ALL_TOOL_NAMES = [
    "get_hub_health",
    "get_pc_status",
    "get_sunshine_status",
    "get_tv_status",
    "get_recent_actions",
    "execute_tv_control",
    "execute_scene",
    "manage_gaming_apps",
    "manage_agent_tasks",
    "manage_project_files",
    "run_approved_script"
]

async def healthz_endpoint(request):
    """Endpoint de observabilidad para verificar disponibilidad de Yedid MCP Gateway."""
    return JSONResponse({
        "status": "healthy",
        "service": "Yedid MCP Gateway",
        "transport": "Streamable HTTP (/mcp)",
        "tools_count": len(ALL_TOOL_NAMES),
        "read_tools": ALL_TOOL_NAMES[:5],
        "action_tools": ALL_TOOL_NAMES[5:],
        "tools": ALL_TOOL_NAMES
    })

def create_gateway_app() -> Starlette:
    """Crea la aplicación ASGI combinada con soporte Streamable HTTP y observabilidad."""
    base_mcp_app = mcp.streamable_http_app()
    base_mcp_app.routes.insert(0, Route("/healthz", endpoint=healthz_endpoint, methods=["GET"]))
    base_mcp_app.add_middleware(MCPAuthMiddleware)
    return base_mcp_app

# App ASGI ejecutable
gateway_app = create_gateway_app()

if __name__ == "__main__":
    import uvicorn
    print("[*] Iniciando Yedid MCP Gateway en http://127.0.0.1:8766/mcp ...")
    uvicorn.run(gateway_app, host="127.0.0.1", port=8766, log_level="info")
