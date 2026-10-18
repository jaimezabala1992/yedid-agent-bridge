# 🌉 Yedid Agent Bridge

Repositorio privado de comunicación asíncrona bidireccional entre **ChatGPT** (asistente personal y coordinador) y los **agentes locales de Yedid Smart Home** (Ollama, Gemini, Codex, Hub).

> [!IMPORTANT]
> **REPOSITORIO ESTRICTAMENTE PRIVADO**  
> Este repositorio funciona como bus de mensajes basado en GitHub Issues.  
> No almacenar secretos, tokens, credenciales ni información confidencial.

---

## 1. ¿Cómo funciona?

1. **ChatGPT crea un Issue** con la tarea en formato JSON estructurado (`yedid-agent-task/1.0`).
2. El **demonio local de Yedid** consulta periódicamente los Issues abiertos (cada 30s) mediante HTTPS saliente.
3. El **Policy Engine** valida el remitente, esquema y permisos locales.
4. El **Agente local seleccionado** (Ollama Qwen3, Gemini, Codex o Yedid Hub) procesa la tarea.
5. El Bridge **publica un comentario** en el Issue con el resultado (`yedid-agent-result/1.0`).
6. **ChatGPT consulta el Issue**, interpreta el resultado y se lo presenta a Jaime.

---

## 2. Formato de Solicitud de Tarea (`yedid-agent-task/1.0`)

Para enviar una tarea a los agentes locales, crea un Issue con el siguiente bloque JSON en la descripción:

```json
{
  "schema": "yedid-agent-task/1.0",
  "task_id": "YEDID_TASK_001",
  "target_agent": "ollama",
  "operation": "respond",
  "priority": "normal",
  "instructions": "Responde únicamente: PONG YEDID",
  "requires_approval": false
}
```

### Agentes disponibles:
- **`ollama`**: Modelo local Qwen3 8B en CPU/RAM. Operaciones: `respond`, `ping`, `summarize`, `explain`.
- **`gemini`**: Diagnósticos de rendimiento, hardware, RAM y telemetría de procesos locales.
- **`codex`**: Revisión técnica de código, parches y propuestas arquitectónicas (modo solo lectura).
- **`yedid_hub`**: Consultas de estado del Hub, dispositivos y telemetría de domótica.

---

## 3. Formato de Respuesta (`yedid-agent-result/1.0`)

El Bridge publicará un comentario con el resultado:

```json
{
  "schema": "yedid-agent-result/1.0",
  "task_id": "YEDID_TASK_001",
  "agent": "ollama",
  "status": "completed",
  "result": "PONG YEDID",
  "requires_followup": false,
  "metadata": {
    "model": "qwen3:8b",
    "execution_time_sec": 9.61
  }
}
```

---

## 4. Políticas de Seguridad

- **Remitentes autorizados**: Únicamente el propietario (`@jaimezabala1992`).
- **Cero terminal remota**: No se permite ejecutar comandos arbitrarios de shell.
- **Human-in-the-Loop (HITL)**: Modificaciones de archivos en disco o acciones sobre dispositivos físicos requieren confirmación explícita desde la PWA de Yedid.
- **Sanitización de fugas**: Respuestas con tokens o credenciales son redactadas automáticamente antes de publicarse.
