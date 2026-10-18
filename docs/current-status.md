# Estado Actual del Proyecto — Yedid Smart Hub v2.0

**Última actualización:** 2026-10-18  
**Versión activa:** Yedid Smart Hub `v2.0.0` (Interfaz Progresiva `PWA v9` / Motor de Memoria `v2.2`)  
**Rama principal:** `main`

---

## 1. Resumen Ejecutivo para Colaboración (Gemini Antigravity + ChatGPT)

El sistema **Yedid Smart Hub v2.0** se encuentra operativo en el entorno local con sus cuatro componentes principales activos:
1. **Hub Principal (`hub.server:app`):** Escuchando en `127.0.0.1:8765` (y publicado de forma segura por HTTPS mediante Tailscale Serve en el puerto `:8443`).
2. **Pasarela MCP (`mcp_gateway.server:gateway_app`):** Escuchando en `127.0.0.1:8766`.
3. **Motor LLM Local (`Ollama`):** Ejecutando `qwen3:8b` en CPU (`127.0.0.1:11434`), manteniendo la GPU dedicada libre para sesiones de streaming con Sunshine/Moonlight.
4. **Motores SQLite Transaccionales (Modo WAL):**
   - `chat_history.db`: Conversaciones multi-turno persistentes, conteo exacto de mensajes e idempotencia por `client_message_id`.
   - `agent_tasks.db`: Motor duradero de ejecución de tareas en segundo plano (`TaskEngine`) vinculado a aprobaciones criptográficas (`ApprovalManager`).
   - `memories_v2.db`: Memoria personal estructurada con búsqueda `FTS5` y cifrado autenticado protegido por Windows DPAPI.

---

## 2. Correcciones Recientes Validadas (Observación Galaxy Z Fold 7 — 2026-10-18)

Se diagnosticaron y resolvieron 5 aspectos observados durante pruebas desde el **Samsung Galaxy Z Fold 7** en la pestaña **Chat** (consulta *«Muéstrame el progreso de los agentes»*):

### 2.1 Síntesis garantizada de resultados de herramientas (`hub/llm_router.py`, `hub/audit.py`)
- **Problema:** Al consultar el progreso de los agentes, se mostraban los chips `get_agent_tasks` y `get_agents_status` pero el cuerpo del mensaje del asistente quedaba vacío (`""`) en SQLite porque `qwen3:8b` agotaba los `450` tokens de salida dentro del bloque interno `thinking` durante la segunda llamada de síntesis. Además, `redact_sensitive()` en `hub/audit.py` filtraba por error cadenas largas que mencionaban `"cuota de tokens"` o la clave `"key_files"`.
- **Solución implementada:**
  - Se añadió `"think": False` y `num_predict: 700` en las llamadas a `/api/chat` de Ollama.
  - Cuando el enrutador determinista de intenciones multiagente ya recolectó la telemetría real en disco (`agents_overview`, `handover_or_resume`, `direct_task_execution`, `gemini_status`, `quota_check`), se evita re-enviar el esquema de herramientas en ese mismo turno para eliminar llamadas duplicadas.
  - Se implementó `_synthesize_tool_results_fallback()` en `hub/llm_router.py` y un respaldo en `formatChatTextHtml()` de `hub/static/index.html`, garantizando que todo resultado de herramienta se convierta en una respuesta clara en español con estados reales y evidencia en disco.
  - Se refinó `redact_sensitive()` en `hub/audit.py` para redactar exclusivamente claves de credenciales reales (`api_key`, `access_token`, `csrf_token`, etc.) y patrones reales de tokens (`Bearer`, `ghp_`, `github_pat_`, `sk-`, `eyJ`).

### 2.2 Sincronización exacta del selector y contador de conversaciones con SQLite (`hub/chat_history_db.py`, `hub/static/index.html`)
- **Problema:** El selector `#select-conversation` seguía mostrando `«Nueva Conversación (0 msgs)»` tras enviar el primer mensaje porque `sendChatMessage()` solo recargaba la lista de conversaciones si `data.conversation_id !== currentConversationId`.
- **Solución implementada:**
  - `get_or_create_conversation()`, `list_conversations()` y `get_conversation()` en `hub/chat_history_db.py` calculan y devuelven siempre `message_count` como entero mediante `SELECT COUNT(*) FROM messages`.
  - Se añadió `refreshConversationSelector()` en `hub/static/index.html`, invocándose automáticamente tras cada turno en `sendChatMessage()`, al cargar mensajes en `loadConversationMessages()` y tras resolver aprobaciones en `resolveApproval()`.

### 2.3 Recuperación confiable al actualizar, cambiar de pestaña y cerrar/reabrir la PWA (`hub/static/index.html`)
- **Problema:** En `DOMContentLoaded`, `checkAuth()` y `loadConversations()` se ejecutaban sin `await`, y `switchTab('chat')` invocaba `loadConversations()` y `loadConversationMessages()` en paralelo.
- **Solución implementada:**
  - `DOMContentLoaded` espera a `await checkAuth()`, el cual invoca `loadConversations()` y `fetchPendingApprovals()` una vez establecida la sesión.
  - Se incorporó un contador de secuencia `_loadMessagesSeq` y `cache: 'no-store'` en `loadConversationMessages()` para descartar respuestas fuera de orden.
  - Se añadieron listeners `visibilitychange` y `pageshow` para refrescar conversaciones, mensajes y aprobaciones al reabrir la PWA o desplegar la pantalla del Galaxy Fold 7.

### 2.4 Ejecución real de tareas duraderas y limpieza de rutas extendidas (`hub/agent_inspector.py`, `hub/security_policy.py`, `hub/tools_registry.py`, `hub/agent_adapters.py`)
- **Problema:** Las rutas almacenadas por Codex en `state_5.sqlite` incluyen el prefijo extendido de Windows `\\?\C:\...`, lo cual provocaba que `Path.relative_to()` en `validate_project_path()` rechazara el directorio al preparar el traspaso (`prepare_task_handover()`).
- **Solución implementada:**
  - `_clean_path()` en `hub/agent_inspector.py` y `validate_project_path()` en `hub/security_policy.py` eliminan de forma determinista los prefijos `\\?\` y `\\?/`.
  - `prepare_task_handover()` y `execute_tool()` propagan `conversation_id` y registran la tarea duradera real en `task_engine.create_task()` (`agent_tasks.db`).
  - `GeminiCLIAdapter.execute_task()` persiste además un reporte verificable en disco (`yedid_reporte_<ts>.md`) cuando una tarea diagnóstica finaliza con código `0` sin haber modificado archivos directamente.
  - `cleanup_orphan_agy_processes()` en `hub/process_guard.py` filtra estrictamente procesos headless (`--sandbox --disable-slash-commands`) cuyo proceso padre ya no exista, protegiendo cualquier sesión interactiva activa.

### 2.5 Identificación explícita de Tarea, Alcance y Riesgo en aprobaciones HITL (`hub/approvals.py`, `hub/static/index.html`)
- **Solución implementada:**
  - `_enrich_record()` en `hub/approvals.py` enriquece toda solicitud pendiente con campos de nivel superior: `task_id` (**Tarea**), `agent` (**Agente**), `scope` (**Alcance / Directorio de proyecto**), `instruction` y `risk_level` / `risk_label` (**Riesgo**), manteniendo intacto el `params_hash` criptográfico.
  - Tanto `#approvals-modal` como `.chat-task-card` en `hub/static/index.html` presentan de forma destacada **Tarea**, **Alcance** y **Riesgo**.

---

## 3. Problemas Conocidos / Limitaciones Actuales

1. **Tiempo de inferencia en CPU (`qwen3:8b`):**
   - Por diseño, Ollama se ejecuta en CPU para reservar el 100% de la GPU a Sunshine/Gaming. Las respuestas conversacionales toman entre `3s` y `12s` dependiendo de la longitud del contexto.
2. **Aprobaciones HITL con expiración de 10 minutos (`600s`):**
   - Las solicitudes de alto riesgo expiran a los 10 minutos por seguridad. Si una tarea queda sin aprobar pasado ese tiempo, `resume_task()` genera automáticamente una nueva solicitud HITL fresca cuando el usuario pulsa **«Reanudar Tarea»**.

---

## 4. Próximas Mejoras Sugeridas para Colaboración con ChatGPT

1. **Streaming SSE opcional en `/api/chat`:** Evaluar entrega progresiva de tokens o estados de herramientas hacia la PWA del Galaxy Fold 7 manteniendo la escritura transaccional al finalizar el turno.
2. **Resumen automático de títulos de conversación:** Generar un título descriptivo corto a partir del primer mensaje del usuario en `chat_history_db.py` en lugar de mantener `'Nueva Conversación'` por defecto.
3. **Panel visual de métricas de `TaskEngine`:** Añadir en la PWA un filtro rápido de historial de tareas duraderas (`completed`, `waiting_approval`, `interrupted`) con visor directo de entregables generados en `sandbox/`.
