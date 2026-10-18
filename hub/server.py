import os
"""Servidor FastAPI robusto, seguro y multiagente de Yedid Hub v2.0."""
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, Response, Depends, Header, Query, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import httpx
from .config import HOST, PORT, AUDIT_LOG_FILE, AUTH_TOKEN, OLLAMA_MODEL, OLLAMA_URL, get_auth_token
from .tv_service import tv_service
from .system_service import get_system_vitals
from .llm_router import router
from .audit import log_event
from .scenes_service import scenes_service, SCENES_CATALOG
from .whisper_service import transcribe_audio_bytes, whisper_manager
from .tts_service import synthesize_speech_sapi
from .auth import session_store, verify_session_or_token, verify_tailscale_peer
from .multiagent_bus import multiagent_bus
from .approvals import approval_manager
from .resource_manager import resource_manager
from .memory_engine import memory_engine, STEPUP_TOKEN_LIFETIME
from .memory_db import memory_db
from .chat_history_db import chat_history_db
from .task_engine import task_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yedid_hub")

app = FastAPI(title="Yedid Smart Home Hub", version="2.0.0")

# Restringir CORS estrictamente al dominio de Tailscale y localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "https://yedid-hub.tailnet.ts.net:8443",
        "https://yedid-hub.tailnet.ts.net"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0"
}

STATIC_DIR = Path(__file__).resolve().parent / "static"

SW_HEADERS = {
    **NO_CACHE_HEADERS,
    "Service-Worker-Allowed": "/"
}

@app.get("/sw.js")
@app.get("/static/sw.js")
async def serve_sw():
    """Service Worker con política estricta de no-cache y permiso de scope raíz."""
    sw_path = STATIC_DIR / "sw.js"
    if sw_path.exists():
        return RawResponse(
            content=sw_path.read_bytes(),
            media_type="application/javascript",
            headers=SW_HEADERS
        )
    return RawResponse(status_code=404)
@app.get("/favicon.ico")
async def serve_favicon():
    """Sirve el icono de la PWA para evitar 404 en navegadores móviles."""
    fav_path = STATIC_DIR / "icons" / "icon-192.png"
    if fav_path.exists():
        return RawResponse(content=fav_path.read_bytes(), media_type="image/png")
    return RawResponse(status_code=404)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
async def startup_event():
    """Inicia watchdog de recursos y motor duradero de tareas con reconciliación."""
    await resource_manager.start_watchdog()
    task_engine.reconcile_on_startup()
    task_engine.start_worker()

@app.on_event("shutdown")
async def shutdown_event():
    """Detiene workers y libera recursos al apagar el servidor."""
    task_engine.stop_worker()
    chat_history_db.checkpoint()
    resource_manager.stop_watchdog()
    whisper_manager.unload_worker()

# Modelos de solicitud
class LoginRequest(BaseModel):
    token: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    client_message_id: Optional[str] = None
    stepup_token: Optional[str] = None

class CreateConversationRequest(BaseModel):
    title: Optional[str] = None

class CreateTaskRequest(BaseModel):
    agent: str = "gemini_cli"
    instruction: str
    project_path: Optional[str] = None
    conversation_id: Optional[str] = None
    risk_level: Optional[str] = "LOW"

class TVKeyRequest(BaseModel):
    key: str

class TVPowerRequest(BaseModel):
    power_on: Optional[bool] = None

class TVLaunchRequest(BaseModel):
    app: str

class SceneRequest(BaseModel):
    scene: str

class TTSRequest(BaseModel):
    text: str

class ProfileChangeRequest(BaseModel):
    profile: str

class UnloadRequest(BaseModel):
    target: Optional[str] = "all"

class MemoryReviewRequest(BaseModel):
    action: str
    stepup_token: Optional[str] = None

class MemoryBatchReviewRequest(BaseModel):
    action: str
    category: Optional[str] = None
    stepup_token: Optional[str] = None

class MemoryCreateRequest(BaseModel):
    category: str
    access_level: str
    title: str
    content: str
    validity: Optional[str] = "permanent"
    status: Optional[str] = "ACTIVE"

class MemoryUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    access_level: Optional[str] = None

class MemoryStepupRequest(BaseModel):
    secret: str

# Endpoints de Autenticación por Sesiones HttpOnly + CSRF
@app.post("/api/auth/login")
async def login_endpoint(req: LoginRequest, request: Request, response: Response):
    """Inicia sesión explícita con token autorizado o mediante Tailscale Serve verificado."""
    client_ip = request.client.host if request.client else "unknown"
    authorized = False
    user = "jaime"

    # 1. Login explícito mediante AUTH_TOKEN o credencial dinámica de .token
    current_token = get_auth_token()
    if req.token:
        if req.token.strip() == current_token:
            authorized = True
            user = "jaime"
        else:
            authorized = False

    # 2. Login transparente por Tailscale Serve ÚNICAMENTE si procede del proxy HTTPS confiable
    #    y se valida criptográficamente la identidad del nodo mediante 'tailscale whois'
    elif not req.token:
        x_forwarded_host = request.headers.get("x-forwarded-host", "")
        x_forwarded_proto = request.headers.get("x-forwarded-proto", "")
        x_forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        tailscale_user = request.headers.get("Tailscale-User-Login")

        if os.getenv("YEDID_TAILSCALE_DOMAIN", "yedid-hub.tailnet.ts.net") in x_forwarded_host and x_forwarded_proto == "https":
            if x_forwarded_for and tailscale_user:
                peer_info = verify_tailscale_peer(x_forwarded_for, claimed_user=tailscale_user)
                if peer_info:
                    authorized = True
                    user = peer_info["login_name"]

    if not authorized:
        log_event("login_failed", {"client_ip": client_ip})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas o identidad de Tailscale Serve no verificada."
        )

    sess_data = session_store.create_session(user=user)
    session_id = sess_data["session_id"]
    csrf_token = sess_data["csrf_token"]

    is_https = (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
        max_age=86400 * 30  # 30 días persistente para Galaxy Fold 7 y PC
    )

    log_event("login_success", {"user": user, "client_ip": client_ip})
    return {"success": True, "csrf_token": csrf_token, "user": user}

@app.post("/api/auth/logout")
async def logout_endpoint(request: Request, response: Response):
    """Cierra la sesión activa y expira la cookie."""
    session_id = request.cookies.get("session_id")
    if session_id:
        session_store.delete_session(session_id)
    response.delete_cookie(key="session_id", path="/")
    return {"success": True, "message": "Sesión cerrada correctamente."}

@app.get("/api/auth/me")
async def auth_me_endpoint(request: Request, response: Response):
    """Verifica si la sesión actual es válida o inicializa la sesión de Tailscale Serve verificada."""
    session_id = request.cookies.get("session_id")
    if session_id:
        sess = session_store.get_session(session_id)
        if sess:
            return {"authenticated": True, "user": sess["user"], "csrf_token": sess["csrf_token"]}

    # Si no hay sesión previa, verificar si proviene legítimamente del proxy Tailscale Serve
    x_forwarded_host = request.headers.get("x-forwarded-host", "")
    x_forwarded_proto = request.headers.get("x-forwarded-proto", "")
    x_forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    tailscale_user = request.headers.get("tailscale-user-login") or request.headers.get("Tailscale-User-Login")
    logger.info(f"DEBUG auth_me: host='{x_forwarded_host}', proto='{x_forwarded_proto}', for='{x_forwarded_for}', user='{tailscale_user}'")

    if (os.getenv("YEDID_TAILSCALE_DOMAIN", "yedid-hub.tailnet.ts.net") in x_forwarded_host or request.headers.get("host") == f"{os.getenv('YEDID_TAILSCALE_DOMAIN', 'yedid-hub.tailnet.ts.net')}:8443") and (x_forwarded_proto == "https" or request.url.scheme == "https"):
        if x_forwarded_for and tailscale_user:
            peer_info = verify_tailscale_peer(x_forwarded_for, claimed_user=tailscale_user)
            if peer_info:
                user = peer_info["login_name"]
                sess_data = session_store.create_session(user=user)
                is_https = (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto") == "https")
                response.set_cookie(
                    key="session_id",
                    value=sess_data["session_id"],
                    httponly=True,
                    secure=is_https,
                    samesite="lax",
                    path="/",
                    max_age=86400 * 30
                )
                return {"authenticated": True, "user": user, "csrf_token": sess_data["csrf_token"]}

    return {"authenticated": False}

# Endpoints Funcionales Protegidos
@app.get("/api/health")
async def health_check():
    """Estado básico de salud (monitoreo liviano sin datos sensibles)."""
    return {
        "status": "online",
        "service": "Yedid Smart Home Hub",
        "version": "2.0.0",
        "model": OLLAMA_MODEL
    }

@app.get("/api/model/info")
async def model_info_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Información del modelo LLM activo."""
    return {
        "model_name": OLLAMA_MODEL,
        "runner": "Ollama 0.40.0 (Local CPU)",
        "hardware": "AMD Ryzen 5 5600X (6C/12T)",
        "gpu_state": "AMD Radeon RX 6700 XT libre para juegos / Sunshine",
        "tool_calling": "Nativo JSON Schema",
        "privacy": "100% Local / $0 Costo"
    }

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Procesa mensajes con Yedid mediante Ollama y persistencia en SQLite."""
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío.")

    user = auth_info.get("user", "jaime")
    multiagent_bus.dispatch_task("client_ui", "ollama_qwen3", "intent_classification", {"prompt": text})
    result = await router.chat(
        text,
        conversation_id=req.conversation_id,
        client_message_id=req.client_message_id,
        stepup_token=req.stepup_token,
        user_id=user
    )
    return result

@app.post("/api/chat/reset")
async def reset_chat_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Crea una nueva conversación limpia para reiniciar el hilo de diálogo."""
    user = auth_info.get("user", "jaime")
    conv = chat_history_db.create_conversation(user_id=user)
    return {"success": True, "message": "Nueva conversación creada.", "conversation": conv}

# Endpoints de Gestión de Conversaciones Persistentes (SQLite)
@app.get("/api/conversations")
async def list_conversations_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Lista las conversaciones activas del usuario autenticado."""
    user = auth_info.get("user", "jaime")
    conversations = chat_history_db.list_conversations(user_id=user)
    return {"conversations": conversations}

@app.post("/api/conversations")
async def create_conversation_endpoint(req: CreateConversationRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Crea una nueva conversación persistente en el servidor."""
    user = auth_info.get("user", "jaime")
    conv = chat_history_db.create_conversation(user_id=user, title=req.title)
    return {"success": True, "conversation": conv}

@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages_endpoint(conversation_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Obtiene el historial completo de mensajes y tareas asociadas a una conversación."""
    user = auth_info.get("user", "jaime")
    conv = chat_history_db.get_conversation(conversation_id, user=user)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversación no encontrada.")

    messages = chat_history_db.get_messages(conversation_id, limit=100)
    tasks = task_engine.list_tasks(conversation_id=conversation_id, limit=20)
    return {
        "conversation": conv,
        "messages": messages,
        "tasks": tasks
    }

@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation_endpoint(conversation_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Elimina una conversación y sus mensajes asociados."""
    user = auth_info.get("user", "jaime")
    ok = chat_history_db.delete_conversation(conversation_id, user=user)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversación no encontrada.")
    return {"success": True, "conversation_id": conversation_id}

@app.get("/api/tv/status")
async def tv_status_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Estado del televisor de la sala."""
    return await tv_service.get_status()

@app.post("/api/tv/power")
async def tv_power_endpoint(req: TVPowerRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Enciende o apaga la TV."""
    if req.power_on is None:
        return await tv_service.send_key("POWER")
    return await tv_service.set_power(req.power_on)

@app.post("/api/tv/key")
async def tv_key_endpoint(req: TVKeyRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Envía teclas de control remoto."""
    return await tv_service.send_key(req.key)

@app.post("/api/tv/launch")
async def tv_launch_endpoint(req: TVLaunchRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Lanza aplicaciones en la TV."""
    return await tv_service.launch_app(req.app)

@app.get("/api/scenes")
async def get_scenes_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Catálogo de escenas domóticas disponibles."""
    return {"scenes": SCENES_CATALOG}

@app.post("/api/scenes/run")
async def run_scene_endpoint(req: SceneRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Ejecuta una escena automatizada de 1 clic."""
    return await scenes_service.execute_scene(req.scene)

@app.post("/api/voice/transcribe")
async def voice_transcribe_endpoint(file: UploadFile = File(...), auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Transcribe audio grabado por el Galaxy Fold 7 usando Whisper 100% local en CPU."""
    async with resource_manager.track_task("whisper"):
        try:
            content = await file.read()
            suffix = Path(file.filename).suffix if file.filename else ".webm"
            if not suffix:
                suffix = ".webm"
            multiagent_bus.dispatch_task("client_ui", "whisper_voice", "transcribe_audio", {"size": len(content)})
            text = transcribe_audio_bytes(content, suffix=suffix, language="es")
            return {"success": True, "text": text}
        except Exception as e:
            logger.error(f"Error transcribiendo voz local: {e}")
            raise HTTPException(status_code=500, detail=f"Error en transcripción local: {str(e)}")

@app.post("/api/voice/synthesize")
async def voice_synthesize_endpoint(req: TTSRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Sintetiza texto a voz local en español usando SAPI5 Helena / offline."""
    try:
        clean_text = req.text.strip()
        if not clean_text:
            raise HTTPException(status_code=400, detail="El texto a sintetizar no puede estar vacío.")
        audio_bytes = synthesize_speech_sapi(clean_text)
        return RawResponse(content=audio_bytes, media_type="audio/wav")
    except Exception as e:
        logger.error(f"Error en síntesis TTS: {e}")
        raise HTTPException(status_code=500, detail=f"Error en TTS local: {str(e)}")

@app.get("/api/system/vitals")
async def system_vitals_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Telemetría del equipo principal."""
    return get_system_vitals()

@app.get("/api/multiagent/status")
async def multiagent_status_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Estado de coordinación multiagente, matriz de permisos y telemetría de agentes."""
    from .agent_inspector import agent_inspector
    return {
        "agents": multiagent_bus.get_agents(),
        "recent_tasks": multiagent_bus.get_recent_tasks(),
        "overview": agent_inspector.get_all_agents_overview()
    }

@app.get("/api/multiagent/tasks")
async def multiagent_tasks_endpoint(
    auth_info: Dict[str, Any] = Depends(verify_session_or_token),
    status: Optional[str] = Query(None)
):
    """Consulta tareas combinadas de Codex, Gemini y el Bus Multiagente."""
    from .agent_inspector import agent_inspector
    codex_data = agent_inspector.inspect_codex_status(limit=10)
    gemini_data = agent_inspector.inspect_gemini_status(limit=5)
    return {
        "bus_tasks": multiagent_bus.list_tasks(status_filter=status),
        "codex_threads": codex_data.get("threads", []),
        "codex_quota_exhausted": codex_data.get("is_quota_exhausted", False),
        "codex_quota_resets_at": codex_data.get("active_quota_resets_at"),
        "gemini_conversations": gemini_data.get("recent_conversations", [])
    }

@app.get("/api/actions/history")
async def actions_history_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token), limit: int = 15):
    """Devuelve las últimas acciones registradas de manera legible para el Galaxy Fold 7."""
    actions = []
    if AUDIT_LOG_FILE.exists():
        try:
            lines = AUDIT_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
            for line in reversed(lines[-50:]):
                try:
                    entry = json.loads(line)
                    ev_type = entry.get("event_type", "")
                    details = entry.get("details", {})
                    ts = entry.get("timestamp", "")

                    desc = ""
                    if ev_type == "tool_call_attempt":
                        desc = f"Herramienta: {details.get('tool')}"
                    elif ev_type == "tv_key_command":
                        desc = f"Control TV tecla: {details.get('key')}"
                    elif ev_type == "tv_launch_app":
                        desc = f"TV App abierta: {details.get('app_requested')}"
                    elif ev_type == "tv_status_check":
                        desc = f"Consulta TV: {'Encendida' if details.get('is_on') else 'En reposo'}"
                    elif ev_type == "scene_execution_success":
                        desc = f"Escena completada: {details.get('scene')}"
                    elif ev_type == "voice_transcription_success":
                        desc = f"Transcripción Whisper ({details.get('duration')}s)"
                    elif ev_type == "tool_call_rejected":
                        desc = f"Acción rechazada: {details.get('tool')}"
                    elif ev_type == "unauthorized_access_attempt":
                        desc = f"Acceso no autorizado bloqueado ({details.get('client_ip')})"

                    if desc:
                        actions.append({
                            "timestamp": ts,
                            "type": ev_type,
                            "description": desc,
                            "success": details.get("success", True) if "success" in details else True
                        })
                        if len(actions) >= limit:
                            break
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error leyendo historial: {e}")

    return {"actions": actions}

# Endpoints de Aprobación Humana (Human-in-the-Loop)
@app.get("/api/approvals/pending")
async def get_pending_approvals_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Lista las solicitudes de autorización humana pendientes (operaciones de alto riesgo)."""
    return {"pending_approvals": approval_manager.get_pending()}

@app.post("/api/approvals/{approval_id}/approve")
async def approve_request_endpoint(approval_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Aprueba una solicitud humana de alto riesgo desde la PWA."""
    if auth_info.get("auth_type") == "localhost_direct":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La autorización de operaciones de alto riesgo requiere autenticación explícita (Sesión HttpOnly + CSRF o Token Bearer)."
        )
    user = auth_info.get("user", "jaime")
    ok = approval_manager.approve(approval_id, approver=user)
    if not ok:
        raise HTTPException(status_code=400, detail="Solicitud no encontrada, expirada o ya resuelta.")
    task_rec = task_engine.on_approval_granted(approval_id)
    return {"success": True, "approval_id": approval_id, "status": "APPROVED", "approver": user, "task": task_rec}

@app.post("/api/approvals/{approval_id}/reject")
async def reject_request_endpoint(approval_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Rechaza una solicitud de alto riesgo desde la PWA."""
    if auth_info.get("auth_type") == "localhost_direct":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El rechazo de operaciones requiere autenticación explícita (Sesión HttpOnly + CSRF o Token Bearer)."
        )
    user = auth_info.get("user", "jaime")
    ok = approval_manager.reject(approval_id, approver=user)
    if not ok:
        raise HTTPException(status_code=400, detail="Solicitud no encontrada, expirada o ya resuelta.")
    task_rec = task_engine.on_approval_rejected(approval_id)
    return {"success": True, "approval_id": approval_id, "status": "REJECTED", "approver": user, "task": task_rec}

# Endpoints de Motor Duradero de Tareas (Task Engine)
@app.get("/api/tasks")
async def list_tasks_endpoint(
    status: Optional[str] = Query(None),
    conversation_id: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    limit: int = Query(50),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Consulta tareas del motor duradero con filtros opcionales."""
    tasks = task_engine.list_tasks(status_filter=status, conversation_id=conversation_id, agent=agent, limit=limit)
    return {"tasks": tasks, "total": len(tasks)}

@app.get("/api/tasks/{task_id}")
async def get_task_endpoint(task_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Detalle completo, progreso y entregables de una tarea específica."""
    task = task_engine.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")
    return {"task": task}

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Cancela una tarea activa, en espera o en cola."""
    ok = task_engine.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="No se pudo cancelar la tarea (no encontrada o ya finalizada).")
    return {"success": True, "task_id": task_id, "message": "Tarea cancelada."}

@app.post("/api/tasks/{task_id}/resume")
async def resume_task_endpoint(task_id: str, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Reanuda de forma segura una tarea interrumpida o fallida sin crear duplicados."""
    try:
        task = task_engine.resume_task(task_id)
        return {"success": True, "task": task, "message": "Tarea reanudada en cola."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tasks/create")
async def create_task_endpoint(req: CreateTaskRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Crea una tarea duradera directamente en el motor de tareas."""
    target_path = req.project_path or r"C:\Yedid-Smart-Home"
    try:
        task = task_engine.create_task(
            agent=req.agent,
            instruction=req.instruction,
            project_path=target_path,
            conversation_id=req.conversation_id,
            risk_level=req.risk_level or "LOW"
        )
        return {"success": True, "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# Endpoints de Gestión Inteligente de Recursos y Memoria RAM/VRAM
@app.get("/api/resources/status")
async def get_resources_status_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Telemetría detallada de RAM, VRAM y estado de carga de modelos LLM y Whisper."""
    return await resource_manager.get_resource_status()

@app.post("/api/resources/profile")
async def change_resource_profile_endpoint(req: ProfileChangeRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Cambia el perfil de recursos activo (equilibrado, gaming, rapido)."""
    try:
        updated = resource_manager.set_profile(req.profile)
        return {"success": True, "active_profile": req.profile, "profile": updated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/resources/unload")
async def force_unload_resources_endpoint(req: Optional[UnloadRequest] = None, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Descarga de inmediato modelos ociosos para liberar el 100% de memoria."""
    result = await resource_manager.unload_all_idle_models()
    return {"success": True, "details": result}

@app.post("/api/resources/preload")
async def preload_model_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Precarga opcional de Qwen3 para calentamiento previo al chat si el perfil lo permite."""
    profile = resource_manager.get_profile()
    if not profile.get("allow_preload", True):
        return {"success": False, "message": f"Precarga desactivada en perfil '{profile['name']}' para proteger Sunshine/juegos."}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": "",
                    "keep_alive": resource_manager.get_current_keep_alive()
                }
            )
            return {"success": True, "message": f"Modelo {OLLAMA_MODEL} precargado exitosamente."}
    except Exception as e:
        return {"success": False, "error": str(e)}

# Endpoints de Yedid Memory Engine v2.2 (Memoria Personal y Privacidad Reforzada)

@app.post("/api/memory/stepup")
async def memory_stepup_endpoint(req: MemoryStepupRequest, auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Valida la credencial del propietario y genera un token temporal de step-up para acceder a recuerdos 'muy_privado'."""
    token = memory_engine.create_stepup_session(req.secret)
    if not token:
        raise HTTPException(status_code=401, detail="Credencial de paso elevado (step-up) incorrecta.")
    return {"success": True, "stepup_token": token, "expires_in_seconds": STEPUP_TOKEN_LIFETIME}

@app.post("/api/memory/stepup/revoke")
async def memory_stepup_revoke_endpoint(
    stepup_token: Optional[str] = Header(None, alias="X-Stepup-Token"),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Revoca inmediatamente un token de step-up activo en el servidor."""
    if stepup_token:
        memory_engine.revoke_stepup_token(stepup_token)
    return {"success": True, "revoked": True}


@app.post("/api/memory/import")
async def memory_import_endpoint(
    request: Request,
    auth_info: Dict[str, Any] = Depends(verify_session_or_token),
    file: Optional[UploadFile] = None
):
    """Importa archivo JSON esquema yedid_memory_seed/1.0 a la cola de revisión PENDING_REVIEW."""
    try:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            uploaded = form.get("file")
            if uploaded and hasattr(uploaded, "read"):
                raw_bytes = await uploaded.read()
                seed_data = json.loads(raw_bytes.decode("utf-8"))
            else:
                raise ValueError("No se encontró el archivo 'file' en los datos del formulario.")
        else:
            seed_data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"JSON inválido o malformado: {str(e)}")

    client_ip = request.client.host if request.client else "unknown"
    try:
        result = memory_engine.import_seed_json(seed_data, source=f"api_{client_ip}")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error importando seed de memoria: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno procesando importación: {str(e)}")

@app.get("/api/memory/stats")
async def memory_stats_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Resumen cuantitativo de recuerdos por estado, categoría y nivel de acceso."""
    return memory_db.get_summary_stats()

@app.get("/api/memory/list")
async def memory_list_endpoint(
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    access_level: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    stepup_token: Optional[str] = Header(None, alias="X-Stepup-Token"),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Lista recuerdos con filtrado estricto por permisos y búsqueda contextual FTS5.
    
    - 'general': siempre visible.
    - 'personal': descifrado para sesión autenticada del propietario.
    - 'muy_privado': descifrado solo si se provee un token de step-up válido.
    """
    has_stepup = memory_engine.is_stepup_valid(stepup_token)
    items = memory_db.list_memories(status=status, category=category, access_level=access_level, decrypt=True)

    if query and query.strip():
        clean_q = query.strip()
        matched_ids = set(memory_db.search_fts(clean_q, limit=100))
        # También permitir coincidencia si el término está en título, categoría o contenido descifrado
        items = [
            it for it in items
            if it["id"] in matched_ids
            or clean_q.lower() in it["title"].lower()
            or clean_q.lower() in it["category"].lower()
            or clean_q.lower() in (it.get("content") or "").lower()
        ]

    # Filtrar o enmascarar 'muy_privado' si no hay step-up
    filtered = []
    for item in items:
        lvl = item.get("access_level")
        if lvl == "muy_privado" and not has_stepup:
            item_copy = dict(item)
            item_copy["content"] = "[REQUIERE_REAUTENTICACION_STEPUP]"
            item_copy["locked"] = True
            filtered.append(item_copy)
        else:
            item["locked"] = False
            filtered.append(item)

    return {"memories": filtered, "total": len(filtered), "has_stepup": has_stepup}

@app.get("/api/memory/export")
async def memory_export_endpoint(
    stepup_token: Optional[str] = Header(None, alias="X-Stepup-Token"),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Exporta los recuerdos en formato estándar yedid_memory_seed/1.0 para respaldo offline seguro."""
    has_stepup = memory_engine.is_stepup_valid(stepup_token)
    items = memory_db.list_memories(decrypt=True)
    exported = []
    for m in items:
        lvl = m.get("access_level")
        if lvl == "muy_privado" and not has_stepup:
            continue
        exported.append({
            "id": m.get("id"),
            "category": m.get("category"),
            "access_level": lvl,
            "title": m.get("title"),
            "content": m.get("content"),
            "validity": m.get("validity", "permanent"),
            "as_of": m.get("as_of", "")
        })
    return {
        "schema": "yedid_memory_seed/1.0",
        "source_type": "jaime_export",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": len(exported),
        "memories": exported
    }

@app.get("/api/memory/{mem_id}")
async def memory_detail_endpoint(
    mem_id: str,
    stepup_token: Optional[str] = Header(None, alias="X-Stepup-Token"),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Consulta un recuerdo específico."""
    mem = memory_db.get_memory_by_id(mem_id, decrypt=True)
    if not mem:
        raise HTTPException(status_code=404, detail="Recuerdo no encontrado.")

    if mem.get("access_level") == "muy_privado":
        if not memory_engine.is_stepup_valid(stepup_token):
            raise HTTPException(status_code=403, detail="Este recuerdo es 'muy_privado' y requiere verificación step-up previa.")

    return mem

@app.post("/api/memory/{mem_id}/review")
async def memory_review_endpoint(
    mem_id: str,
    req: MemoryReviewRequest,
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Aprueba o descarta un recuerdo individual en revisión."""
    user = auth_info.get("user", "jaime")
    result = memory_engine.review_memory(mem_id, req.action, approver=user, stepup_token=req.stepup_token)
    if not result.get("success"):
        if result.get("requires_stepup"):
            raise HTTPException(status_code=403, detail=result.get("error"))
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result

@app.post("/api/memory/review_all")
async def memory_review_all_endpoint(
    req: MemoryBatchReviewRequest,
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Aprueba o descarta recuerdos pendientes en lote."""
    user = auth_info.get("user", "jaime")
    result = memory_engine.review_batch(req.action, category=req.category, approver=user, stepup_token=req.stepup_token)
    return result

@app.put("/api/memory/{mem_id}")
async def memory_update_endpoint(
    mem_id: str,
    req: MemoryUpdateRequest,
    stepup_token: Optional[str] = Header(None, alias="X-Stepup-Token"),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Edita el contenido o clasificación de un recuerdo existente con control estricto de autorización."""
    existing = memory_db.get_memory_by_id(mem_id, decrypt=False)
    if not existing:
        raise HTTPException(status_code=404, detail="Recuerdo no encontrado.")

    if existing.get("access_level") == "muy_privado" or req.access_level == "muy_privado":
        if not memory_engine.is_stepup_valid(stepup_token):
            raise HTTPException(
                status_code=403,
                detail="Modificar o reclasificar un recuerdo 'muy_privado' requiere autorización previa de paso elevado (Step-Up)."
            )

    try:
        updated = memory_db.update_memory(
            mem_id=mem_id,
            title=req.title,
            content=req.content,
            category=req.category,
            access_level=req.access_level
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Recuerdo no encontrado.")
        return {"success": True, "memory": updated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/memory/{mem_id}")
async def memory_delete_endpoint(
    mem_id: str,
    stepup_token: Optional[str] = Header(None, alias="X-Stepup-Token"),
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Elimina permanentemente un recuerdo de la base de datos y del índice FTS5."""
    existing = memory_db.get_memory_by_id(mem_id, decrypt=False)
    if not existing:
        raise HTTPException(status_code=404, detail="Recuerdo no encontrado.")

    if existing.get("access_level") == "muy_privado":
        if not memory_engine.is_stepup_valid(stepup_token):
            raise HTTPException(
                status_code=403,
                detail="Eliminar un recuerdo 'muy_privado' requiere autorización previa de paso elevado (Step-Up)."
            )

    deleted = memory_db.delete_memory(mem_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recuerdo no encontrado.")
    return {"success": True, "id": mem_id, "message": "Recuerdo eliminado definitivamente."}

@app.post("/api/memory/create")
async def memory_create_endpoint(
    req: MemoryCreateRequest,
    auth_info: Dict[str, Any] = Depends(verify_session_or_token)
):
    """Crea un recuerdo directamente desde la interfaz PWA."""
    mem_id = f"mem_{secrets.token_hex(6)}"
    try:
        created = memory_db.insert_memory(
            mem_id=mem_id,
            category=req.category,
            access_level=req.access_level,
            title=req.title,
            content=req.content,
            validity=req.validity or "permanent",
            origin="pwa_direct",
            review_required=(req.status == "PENDING_REVIEW"),
            status=req.status or "ACTIVE"
        )
        return {"success": True, "memory": created}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creando memoria directa: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Endpoints de Yedid Agent Bridge ---

@app.get("/api/bridge/status")
async def bridge_status_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Retorna el estado de ejecución, configuración y estadísticas de Yedid Agent Bridge."""
    try:
        from agent_bridge.config import load_bridge_config, get_github_token, is_bridge_paused, LOCK_FILE
        from agent_bridge.task_store import TaskStore
        import psutil

        cfg = load_bridge_config()
        token = get_github_token()
        is_running = False
        pid = None
        if LOCK_FILE.exists():
            try:
                pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
                is_running = psutil.pid_exists(pid)
            except Exception:
                pass

        store = TaskStore()
        stats = store.get_stats()

        return {
            "status": "online" if is_running else "stopped",
            "is_running": is_running,
            "pid": pid,
            "is_paused": is_bridge_paused(),
            "has_token": bool(token),
            "repo_owner": cfg.get("repo_owner", ""),
            "repo_name": cfg.get("repo_name", "yedid"),
            "poll_interval_seconds": cfg.get("poll_interval_seconds", 30),
            "stats": stats
        }
    except Exception as e:
        logger.error(f"Error consultando estado del Bridge: {e}")
        return {"status": "error", "error": str(e)}

@app.post("/api/bridge/toggle")
async def bridge_toggle_endpoint(auth_info: Dict[str, Any] = Depends(verify_session_or_token)):
    """Pausa o reanuda la operación del Bridge desde la PWA."""
    try:
        from agent_bridge.config import is_bridge_paused, set_bridge_paused
        current = is_bridge_paused()
        set_bridge_paused(not current)
        return {"success": True, "is_paused": not current}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)

async def serve_index():
    """Página web del Hub con cabeceras anti-caché para Galaxy Fold 7 y PWA."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(
            content=index_path.read_text(encoding="utf-8"),
            headers=NO_CACHE_HEADERS
        )
    return HTMLResponse(content="<h1>Yedid Hub Operativo</h1>", headers=NO_CACHE_HEADERS)
