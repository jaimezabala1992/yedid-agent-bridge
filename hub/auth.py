"""
Módulo de autenticación y autorización para Yedid Hub.
Provee:
1. Almacenamiento y validación de sesiones seguras HttpOnly con tokens CSRF (30 días persistente).
2. Validación criptográfica de identidad de nodos Tailscale mediante 'tailscale whois'.
3. Control estricto de acceso a endpoints: rechaza solicitudes sin sesión válida o token Bearer.
"""
import os
import shutil
import subprocess
import json
import secrets
import time
from typing import Any, Dict, Optional
from fastapi import Request, HTTPException, status, Header, Query, Depends
from .config import AUTH_TOKEN
from .audit import log_event

class SessionStore:
    def __init__(self, ttl_seconds: int = 86400 * 30):  # 30 días de persistencia
        self.ttl_seconds = ttl_seconds
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def create_session(self, user: str = "jaime") -> Dict[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.ttl_seconds
        self._sessions[session_id] = {
            "user": user,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "created_at": time.time()
        }
        return {"session_id": session_id, "csrf_token": csrf_token}

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        sess = self._sessions.get(session_id)
        if not sess:
            return None
        if time.time() > sess["expires_at"]:
            del self._sessions[session_id]
            return None
        return sess

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

session_store = SessionStore()

def verify_tailscale_peer(peer_ip: str, claimed_user: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Verifica criptográficamente un nodo de Tailscale consultando al daemon local mediante 'tailscale whois'.
    Retorna el perfil verificado del usuario si es auténtico, o None si no pertenece a la tailnet.
    """
    if not peer_ip:
        return None
    tailscale_bin = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not os.path.exists(tailscale_bin):
        return None
    try:
        proc = subprocess.run(
            [tailscale_bin, "whois", "--json", peer_ip],
            capture_output=True,
            text=True,
            timeout=3
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        data = json.loads(proc.stdout)
        user_profile = data.get("UserProfile", {})
        login_name = user_profile.get("LoginName", "").lower()
        node = data.get("Node", {})
        is_online = node.get("Online", False) or node.get("MachineAuthorized", False)
        
        # Validar que pertenezca al usuario Jaime Zabala y el nodo esté activo
        if ("jaime_zabala20" in login_name or "jaime" in login_name) and is_online:
            if claimed_user and claimed_user.lower() not in login_name:
                return None
            return {
                "verified": True,
                "login_name": login_name,
                "display_name": user_profile.get("DisplayName", "Jaime"),
                "node_name": node.get("ComputedName", "")
            }
        return None
    except Exception:
        return None

def verify_session_or_token(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token")
) -> Dict[str, Any]:
    """
    Verifica estrictamente la identidad del cliente mediante:
    1. Cookie de sesión HttpOnly ('session_id') válida y activa en session_store.
       (Valida token CSRF en métodos mutantes POST, PUT, DELETE, PATCH).
    2. Token Bearer estricto ('Authorization: Bearer <AUTH_TOKEN>') para APIs y CLI.
    
    ESTRICTO: Las cabeceras como 'Tailscale-User-Login' o pertenecer a la subred 100.64.0.0/10 o localhost
    NO otorgan autorización a endpoints funcionales o datos privados sin una sesión válida.
    """
    client_ip = request.client.host if request.client else "unknown"

    # 1. Comprobar sesión por Cookie HttpOnly
    session_id = request.cookies.get("session_id")
    if session_id:
        sess = session_store.get_session(session_id)
        if sess:
            # En peticiones que modifican estado (POST, PUT, DELETE, PATCH), validar CSRF
            if request.method in ("POST", "PUT", "DELETE", "PATCH"):
                if not x_csrf_token or x_csrf_token != sess["csrf_token"]:
                    log_event("csrf_validation_failed", {
                        "path": request.url.path,
                        "client_ip": client_ip,
                        "reason": "Token CSRF faltante o no coincide"
                    })
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Fallo de validación CSRF. Encabezado X-CSRF-Token requerido."
                    )
            return {"auth_type": "cookie_session", "user": sess["user"], "csrf_token": sess["csrf_token"]}

    # 2. Comprobar Token Bearer para peticiones de API / CLI
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
        from .config import get_auth_token
        if token == get_auth_token():
            return {"auth_type": "bearer_token", "user": "api_client"}
        else:
            log_event("invalid_bearer_token", {"path": request.url.path, "client_ip": client_ip})
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token de acceso inválido."
            )

    # Cualquier petición sin sesión válida ni Bearer token es rechazada (incluso con cabeceras falsificadas)
    log_event("unauthorized_access_attempt", {
        "path": request.url.path,
        "method": request.method,
        "client_ip": client_ip
    })
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Autenticación requerida. Se requiere una sesión válida o token Bearer autorizado."
    )
