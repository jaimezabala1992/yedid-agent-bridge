# Catálogo de Componentes y Funcionalidades — Yedid Smart Hub v2.0

Este documento detalla cada paquete, módulo, clase y endpoint principal de **Yedid Smart Hub** para facilitar la navegación del código por parte de ChatGPT y Gemini Antigravity.

---

## 1. Paquete Principal: `hub/`

| Módulo | Clases / Funciones Clave | Responsabilidad |
| :--- | :--- | :--- |
| `hub/server.py` | `app` (FastAPI), endpoints `/api/*` | Servidor HTTP principal (puerto `8765`), middleware de cabeceras de seguridad (CSP, `X-Frame-Options`), sesiones HttpOnly, validación CSRF y ciclo de vida (`startup_event` / `shutdown_event`). |
| `hub/llm_router.py` | `LLMRouter`, `_clean_llm_output()` | Orquestador conversacional multi-turno respaldado en `chat_history.db`, detección determinista de intenciones multiagente, invocación de herramientas y síntesis determinista de respaldo (`_synthesize_tool_results_fallback()`). |
| `hub/task_engine.py` | `TaskEngine`, `task_engine` | Motor duradero de tareas asíncronas en SQLite (`data/agent_tasks.db`). Gestiona `create_task()`, `on_approval_granted()`, `on_approval_rejected()`, `resume_task()`, `cancel_task()`, `reconcile_on_startup()` y el worker en segundo plano (`_worker_loop()`). |
| `hub/chat_history_db.py` | `ChatHistoryDB`, `chat_history_db` | Persistencia transaccional en SQLite (`data/chat_history.db`, modo WAL) para conversaciones y mensajes, con conteo real de mensajes (`message_count`) e idempotencia por `client_message_id`. |
| `hub/agent_inspector.py` | `AgentInspector`, `agent_inspector`, `_clean_path()` | Telemetría en disco de sesiones de Codex (`state_5.sqlite` y rollouts JSONL), detección de cuota agotada (`usage_limit_exceeded`) con hora de reinicio, estado de Gemini CLI y preparación de traspaso de tareas (`prepare_task_handover()`). |
| `hub/agent_adapters.py` | `GeminiCLIAdapter`, `SafeScriptAdapter`, `scan_deliverables()` | Adaptadores de ejecución supervisada en subprocesos aislados (`--mode accept-edits --sandbox --disable-slash-commands`), auditoría de entregables en disco y persistencia automática de reportes de ejecución. |
| `hub/approvals.py` | `ApprovalManager`, `canonical_params_hash()` | Sistema Human-in-the-Loop (HITL) que retiene tareas de riesgo `HIGH`, calcula huella SHA-256 canónica sobre parámetros y expone explícitamente **Tarea (`task_id`)**, **Agente (`agent`)**, **Alcance (`scope`)** y **Riesgo (`risk_level` / `risk_label`)**. |
| `hub/security_policy.py` | `validate_project_path()`, `is_sensitive_path()`, `classify_task_risk()`, `verify_deliverables_security()` | Lista blanca de raíces de proyectos autorizados, bloqueo de zonas rojas (`memories_v2.db`, `.token`, `backups/`, directorios del sistema), sanitización de instrucciones y entorno limpio para agentes. |
| `hub/process_guard.py` | `WindowsJobGuard`, `process_guard` | Creación y control de Job Objects del kernel de Windows para agrupar y terminar árboles de procesos hijos sin dejar procesos huérfanos ni afectar sesiones interactivas activas. |
| `hub/tools_registry.py` | `TOOLS_SCHEMA`, `execute_tool()` | Registro declarativo JSON Schema de herramientas disponibles para el LLM (`tv_*`, `pc_get_status`, `propose_memory_to_save`, `get_agents_status`, `get_agent_tasks`, `inspect_codex_project`, `request_task_handover`, `execute_agent_task`). |
| `hub/memory_engine.py` | `MemoryEngine`, `memory_engine` | Lógica de negocio de la Memoria Personal v2.2: propuestas desde chat, aprobación/descarte, niveles de acceso (`normal`, `personal`, `sensitive`), exportación/importación segura y auditoría. |
| `hub/memory_db.py` | `MemoryDB` | Almacenamiento SQLite (`data/memories_v2.db`) con tablas relacionales, índice de búsqueda de texto completo `FTS5` y cifrado de campos sensibles. |
| `hub/memory_crypto.py` | `MemoryCrypto`, `_dpapi_protect()`, `_dpapi_unprotect()` | Cifrado autenticado respaldado por Windows DPAPI (`CryptProtectData`) para proteger la clave maestra de memoria en reposo. |
| `hub/auth.py` | `SessionStore`, `verify_tailscale_peer()`, `verify_session_or_token()` | Autenticación híbrida: identidad verificada de Tailscale Serve (rango CGNAT `100.64.0.0/10`), sesiones con cookies `HttpOnly` + token `X-CSRF-Token`, token maestro Bearer y tokens temporales *Step-Up* para memoria sensible. |
| `hub/audit.py` | `log_event()`, `redact_sensitive()` | Bitácora estructurada en `data/audit.jsonl` con redacción automática de claves de credenciales sin corromper metadatos legítimos (`key_files`, explicaciones de cuota). |
| `hub/resource_manager.py` | `ResourceManager`, `resource_manager` | Perro guardián de recursos (CPU/RAM) y gestión dinámica del `keep_alive` de Ollama para liberar memoria cuando Sunshine/Gaming está activo. |
| `hub/tv_service.py` | `TVService`, `tv_service` | Integración asíncrona con Google TV / Android TV mediante `androidtvremote2` (encendido/apagado, volumen, navegación D-Pad y lanzamiento directo de apps). |
| `hub/system_service.py` | `get_system_vitals()` | Telemetría en tiempo real de CPU, RAM, almacenamiento y procesos del equipo anfitrión mediante `psutil`. |
| `hub/gaming_service.py` | `GamingService`, `gaming_service` | Supervisión y control del servidor de streaming **Sunshine** para sesiones de juego remoto vía Moonlight. |
| `hub/scenes_service.py` | `ScenesService`, `scenes_service` | Ejecución de escenas domóticas de 1 clic (`Modo Cine`, `Modo Juego`, `Modo Trabajo`, `Buenas Noches`). |
| `hub/whisper_service.py` & `whisper_worker.py` | `WhisperManager`, `transcribe_audio_bytes()` | Transcripción de voz 100% local con `faster-whisper` (`tiny`, `int8` en CPU) ejecutada en un proceso worker independiente con descarga automática tras inactividad. |
| `hub/tts_service.py` | `synthesize_speech_sapi()` | Síntesis de voz local offline mediante Windows SAPI5 con fallback a Web Speech API en el cliente. |

---

## 2. Interfaz Progresiva (PWA v9): `hub/static/`

- **`hub/static/index.html`:** Aplicación de página única (SPA) sin dependencias externas de compilación. Incluye:
  - Pestañas **Chat**, **TV**, **PC & Gaming**, **Memoria v2.2** e **Historial**.
  - Selector persistente de conversaciones SQLite (`#select-conversation`) sincronizado mediante `refreshConversationSelector()`.
  - Tarjetas interactivas de tareas duraderas (`.chat-task-card`) con estado en tiempo real, alcance, riesgo, entregables y botones de **Autorizar y Ejecutar**, **Rechazar** o **Reanudar Tarea**.
  - Modal de **Aprobaciones Pendientes (HITL)** (`#approvals-modal`) que muestra claramente **Tarea**, **Alcance** y **Riesgo**.
  - Watchdog del DOM (`startDOMWatchdog()`) y botón de reparación de emergencia (`emergencyRepairApp()`) para limpiar cachés de Service Worker en móviles si fuera necesario.
- **`hub/static/sw.js`:** Service Worker v9 con estrategia *Network-First* para documentos HTML y llamadas `/api/*` (nunca almacena en caché respuestas de API ni historiales de chat).

---

## 3. Pasarela MCP: `mcp_gateway/`

- **`mcp_gateway/server.py`:** Servidor FastMCP en `127.0.0.1:8766` con middleware `MCPAuthMiddleware`.
- **`mcp_gateway/tools.py`:** Expone herramientas seguras de lectura y control hacia clientes MCP autorizados (`get_hub_health`, `get_pc_status`, `get_tv_status`, `execute_tv_control`, `execute_scene`, ` list_sandbox_files`, `read_sandbox_file`, `write_sandbox_file`).
- **`mcp_gateway/file_sandbox.py`:** Aislamiento estricto de operaciones de archivo dentro del directorio `sandbox/` y `workspace/`.

---

## 4. Puente de Agentes en GitHub: `agent_bridge/`

- **`agent_bridge/bridge_daemon.py` & `dispatcher.py`:** Demonio opcional de sondeo saliente HTTPS que sincroniza tareas estructuradas con GitHub.
- **`agent_bridge/schemas.py` & `security.py`:** Validación estricta de esquemas JSON (`AgentTask`, `AgentResult`) y motor de políticas contra inyección de comandos.
- **`agent_bridge/adapters/`:** Adaptadores modulares para `gemini_cli`, `codex`, `ollama` y `hub`.

---

## 5. Resumen de Endpoints HTTP Principales (`hub/server.py`)

| Método y Ruta | Descripción |
| :--- | :--- |
| `GET /api/health` | Estado de salud del Hub y modelo LLM activo. |
| `POST /api/auth/login` / `GET /api/auth/me` | Inicio y verificación de sesión + emisión de `csrf_token`. |
| `POST /api/chat` | Envío de mensaje conversacional con persistencia en SQLite, ejecución de herramientas y retorno de `reply`, `tools_executed`, `task` y `conversation`. |
| `GET /api/conversations` | Lista conversaciones activas en SQLite con su `message_count` exacto. |
| `POST /api/conversations` / `DELETE /api/conversations/{id}` | Creación y eliminación de conversaciones en SQLite. |
| `GET /api/conversations/{id}/messages` | Recupera metadata de la conversación, lista completa de mensajes y tareas vinculadas (`tasks`). |
| `GET /api/agents/overview` | Resumen ejecutivo en tiempo real de todos los agentes, tareas duraderas y aprobaciones pendientes. |
| `POST /api/tasks/create` / `GET /api/tasks` | Creación directa y listado de tareas duraderas en `TaskEngine`. |
| `GET /api/tasks/{id}` / `POST /api/tasks/{id}/resume` / `POST /api/tasks/{id}/cancel` | Consulta de estado, reanudación inteligente o cancelación con terminación de Job Object. |
| `GET /api/approvals/pending` | Lista solicitudes HITL pendientes con `task_id`, `agent`, `scope`, `instruction`, `risk_level` y `risk_label`. |
| `POST /api/approvals/{id}/approve` / `POST /api/approvals/{id}/reject` | Autoriza (y encola automáticamente en `TaskEngine`) o rechaza una solicitud HITL. |
