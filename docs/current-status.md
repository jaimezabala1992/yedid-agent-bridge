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

## 3. Auditoría Técnica y Corrección de los 6 Hallazgos de ChatGPT (Yedid) — Rama `fix/chatgpt-audit-6-findings`

Tras la primera revisión arquitectónica de **ChatGPT (Yedid)** sobre `jaimezabala1992/yedid-agent-bridge`, se auditaron los 6 puntos reportados en un entorno aislado con bases de datos temporales (`tests/test_chatgpt_audit_fixes.py`), confirmando que **los 6 correspondían a defectos reales** y aplicando sus correcciones:

1. **Consultas SQL parametrizadas sin `os.getenv()` literal (`hub/chat_history_db.py`):**
   - **Causa:** El sanitizador inicial reemplazó el correo del propietario dentro de cadenas SQL triples por `os.getenv('YEDID_OWNER_EMAIL', ...)`, provocando que SQLite intentara evaluar `os.getenv` como función SQL en `list_conversations()`, `get_conversation()` y `delete_conversation()`.
   - **Corrección:** Se introdujo `ChatHistoryDB._owner_email()` en Python y se vinculó como parámetro SQL `?` (`c.user IN ('jaime', ?, 'api_client')`).

2. **Neutralización de inyección HTML/JavaScript (XSS) en la PWA (`hub/static/index.html`):**
   - **Causa:** `formatChatTextHtml()` omitía el escapado HTML si el texto contenía `<div` o `<strong`, permitiendo vectores como `<div onmouseover=...>` o `<strong onclick=...>`. Además, `renderTaskCardHtml()`, `renderApprovalsModal()` y `appendMessage()` interpolaban campos dinámicos directamente en `innerHTML`.
   - **Corrección:** Se implementó `escapeHtml(val)` y se eliminó la exención condicional en `formatChatTextHtml()`, escapando siempre de forma incondicional antes de aplicar las reglas Markdown seguras (`**negrita**`, `` `código` ``, saltos de línea), y escapando todos los campos dinámicos en tarjetas de tareas, modales HITL, historial y listas de memoria.

3. **Eliminación de riesgo de inyección de código en `SafeScriptAdapter` (`hub/agent_adapters.py`):**
   - **Causa:** `SafeScriptAdapter.execute_task()` generaba un script Python mediante un `f-string` que incrustaba `{sanitized_instruction}` dentro de una cadena triple `f'''...'''`. Si la instrucción contenía `'''` o expresiones `{...}`, podía romper el literal e inyectar código Python arbitrario.
   - **Corrección:** Se reemplazó la interpolación en código fuente por una **plantilla Python 100% estática** y paso de parámetros exclusivamente mediante variables de entorno inertes (`YEDID_TASK_INSTRUCTION`, `YEDID_TASK_OUTPUT_PATH`, `YEDID_TASK_PROJECT_DIR`).

4. **Recuperación correcta de los turnos más recientes en `get_recent_context_for_llm()` (`hub/chat_history_db.py`):**
   - **Causa:** `get_recent_context_for_llm(limit)` delegaba en `get_messages(limit=limit * 2)`, cuya consulta usaba `ORDER BY created_at ASC LIMIT ?`. Al superar `limit * 2` mensajes, devolvía los mensajes **más antiguos** del inicio de la conversación en lugar de los más recientes.
   - **Corrección:** Se implementó una subconsulta que selecciona los `limit * 2` mensajes más recientes (`ORDER BY created_at DESC, rowid DESC LIMIT ?`) y los reordena cronológicamente (`ORDER BY created_at ASC, rowid ASC`).

5. **Deduplicación determinista por `client_message_id` (`hub/chat_history_db.py`, `hub/llm_router.py`):**
   - **Causa:** Al detectar un `client_message_id` duplicado, `LLMRouter.chat()` consultaba `get_messages(conversation_id, limit=2)` (que traía los 2 mensajes más antiguos de la conversación) y devolvía `task: None`.
   - **Corrección:** Se añadió `ChatHistoryDB.get_response_for_client_message_id(client_message_id, conversation_id)`, que localiza por `rowid` y `created_at` el mensaje de usuario exacto y devuelve la respuesta del asistente generada en ese turno junto con su tarea vinculada (`task_engine.get_task(task_id)`).

6. **Prevención de condiciones de carrera entre `cancel_task()` y `_execute_task_pipeline()` (`hub/task_engine.py`):**
   - **Causa:** Si `cancel_task()` se invocaba mientras una tarea estaba en `running`, al terminar el proceso hijo (con código de salida distinto de cero por `kill()`), `_execute_task_pipeline()` ejecutaba `UPDATE tasks SET status = 'failed' WHERE id = ?`, sobrescribiendo el estado `'cancelled'`. Asimismo, una tarea cancelada justo tras ser leída como `queued` podía pasar a `running`.
   - **Corrección:**
     - Se añadió verificación temprana `_is_task_cancelled(task_id)` y transición atómica *Compare-And-Set* (`UPDATE tasks SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'`).
     - Al finalizar el adaptador, el `UPDATE` final incluye `WHERE id = ? AND status != 'cancelled'`. Si la fila ya estaba en `'cancelled'`, se preserva intacto `status = 'cancelled'` y solo se registran los metadatos de cierre (`duration_seconds`, `exit_code`, `stdout`, `stderr`).

---

## 4. Problemas Conocidos / Limitaciones Actuales

1. **Tiempo de inferencia en CPU (`qwen3:8b`):**
   - Por diseño, Ollama se ejecuta en CPU para reservar el 100% de la GPU a Sunshine/Gaming. Las respuestas conversacionales toman entre `3s` y `12s` dependiendo de la longitud del contexto.
2. **Aprobaciones HITL con expiración de 10 minutos (`600s`):**
   - Las solicitudes de alto riesgo expiran a los 10 minutos por seguridad. Si una tarea queda sin aprobar pasado ese tiempo, `resume_task()` genera automáticamente una nueva solicitud HITL fresca cuando el usuario pulsa **«Reanudar Tarea»**.

---

## 5. Próximas Mejoras Sugeridas para Colaboración con ChatGPT

1. **Streaming SSE opcional en `/api/chat`:** Evaluar entrega progresiva de tokens o estados de herramientas hacia la PWA del Galaxy Fold 7 manteniendo la escritura transaccional al finalizar el turno.
2. **Resumen automático de títulos de conversación:** Generar un título descriptivo corto a partir del primer mensaje del usuario en `chat_history_db.py` en lugar de mantener `'Nueva Conversación'` por defecto.
3. **Panel visual de métricas de `TaskEngine`:** Añadir en la PWA un filtro rápido de historial de tareas duraderas (`completed`, `waiting_approval`, `interrupted`) con visor directo de entregables generados en `sandbox/`.

