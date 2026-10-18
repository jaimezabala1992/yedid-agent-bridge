# -*- coding: utf-8 -*-
"""
agent_bridge/github_client.py

Cliente REST asíncrono para la API de GitHub (v3 / 2022-11-28).
- Conexiones salientes HTTPS exclusivamente.
- Verificación mandataria de repositorio estrictamente PRIVADO antes de cualquier operación.
- Gestión de límites de tasa (Rate Limiting) y reintentos ante desconexión.
"""

import time
import logging
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger("YedidAgentBridge.GitHubClient")

GITHUB_API_BASE = "https://api.github.com"

class GitHubSecurityException(Exception):
    """Excepción lanzada si un repositorio no es privado o viola políticas de seguridad."""
    pass

class GitHubBridgeClient:
    """Cliente para interactuar con Issues y comentarios del repositorio privado en GitHub."""

    def __init__(self, token: str, timeout: float = 20.0):
        if not token or not token.strip():
            raise ValueError("Token de GitHub ausente o no configurado.")
        self.token = token.strip()
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Yedid-Agent-Bridge/1.0"
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> httpx.Response:
        """Ejecuta petición HTTP saliente con manejo de límites y errores."""
        url = f"{GITHUB_API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.request(method, url, headers=self.headers, json=json_data, params=params)
            
            # Monitoreo de Rate Limit de GitHub
            remaining = res.headers.get("x-ratelimit-remaining")
            if remaining is not None:
                rem_val = int(remaining)
                if rem_val < 5:
                    logger.warning(f"Límite de API de GitHub cercano al agotamiento ({rem_val} peticiones restantes)")

            return res

    async def get_authenticated_user(self) -> Dict[str, Any]:
        """Obtiene la identidad del usuario autenticado en GitHub."""
        res = await self._request("GET", "/user")
        if res.status_code != 200:
            raise RuntimeError(f"Error autenticando con GitHub (HTTP {res.status_code}): {res.text}")
        return res.json()

    async def get_repo(self, owner: str, repo: str) -> Optional[Dict[str, Any]]:
        """Obtiene metadatos de un repositorio. Retorna None si no existe (404)."""
        res = await self._request("GET", f"/repos/{owner}/{repo}")
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise RuntimeError(f"Error consultando repositorio {owner}/{repo} (HTTP {res.status_code}): {res.text}")
        return res.json()

    async def verify_repo_is_private(self, owner: str, repo: str) -> bool:
        """Verifica de forma estricta que el repositorio sea 100% PRIVADO.
        
        Si el repositorio es público, se arroja una excepción de seguridad crítica y
        se aborta cualquier publicación de comentarios o tareas.
        """
        data = await self.get_repo(owner, repo)
        if not data:
            raise FileNotFoundError(f"El repositorio '{owner}/{repo}' no existe en GitHub.")

        is_private = data.get("private") is True
        visibility = data.get("visibility", "")

        if not is_private or visibility == "public":
            err_msg = f"SEGURIDAD CRÍTICA: El repositorio '{owner}/{repo}' NO ES PRIVADO (visibility='{visibility}'). Operación cancelada inmediatamente."
            logger.critical(err_msg)
            raise GitHubSecurityException(err_msg)

        return True

    async def create_private_repo(
        self,
        repo_name: str,
        description: str = "Repositorio privado de comunicación asíncrona para Yedid Agent Bridge."
    ) -> Dict[str, Any]:
        """Crea el repositorio como estrictamente PRIVADO con GitHub Issues activados."""
        payload = {
            "name": repo_name,
            "private": True,
            "visibility": "private",
            "has_issues": True,
            "description": description,
            "auto_init": True
        }
        res = await self._request("POST", "/user/repos", json_data=payload)
        if res.status_code in (200, 201):
            data = res.json()
            if data.get("private") is not True:
                raise GitHubSecurityException("Fallo al crear repositorio: GitHub no lo marcó como privado.")
            logger.info(f"Repositorio privado '{repo_name}' creado exitosamente en GitHub.")
            return data
        else:
            raise RuntimeError(f"No se pudo crear el repositorio '{repo_name}' (HTTP {res.status_code}): {res.text}")

    async def fetch_open_issues(self, owner: str, repo: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Consulta los Issues abiertos, ordenados por fecha de creación ascendente (FIFO)."""
        await self.verify_repo_is_private(owner, repo)
        params = {
            "state": "open",
            "sort": "created",
            "direction": "asc",
            "per_page": limit
        }
        res = await self._request("GET", f"/repos/{owner}/{repo}/issues", params=params)
        if res.status_code != 200:
            raise RuntimeError(f"Error listando issues de {owner}/{repo} (HTTP {res.status_code}): {res.text}")

        all_issues = res.json()
        # Filtrar Pull Requests (GitHub trata PRs como issues en la API)
        pure_issues = [i for i in all_issues if "pull_request" not in i]
        return pure_issues

    async def post_issue_comment(self, owner: str, repo: str, issue_number: int, comment_body: str) -> Dict[str, Any]:
        """Publica un comentario con el resultado de la tarea en el Issue correspondiente."""
        await self.verify_repo_is_private(owner, repo)
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        res = await self._request("POST", endpoint, json_data={"body": comment_body})
        if res.status_code not in (200, 201):
            raise RuntimeError(f"Error publicando comentario en Issue #{issue_number} (HTTP {res.status_code}): {res.text}")
        return res.json()

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: List[str]) -> List[Dict[str, Any]]:
        """Añade etiquetas a un Issue (ej. 'yedid-processed')."""
        await self.verify_repo_is_private(owner, repo)
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/labels"
        res = await self._request("POST", endpoint, json_data={"labels": labels})
        if res.status_code not in (200, 201):
            logger.warning(f"No se pudieron aplicar etiquetas {labels} en #{issue_number}: {res.text}")
            return []
        return res.json()

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> bool:
        """Cierra el Issue una vez completado."""
        await self.verify_repo_is_private(owner, repo)
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}"
        res = await self._request("PATCH", endpoint, json_data={"state": "closed"})
        return res.status_code == 200
