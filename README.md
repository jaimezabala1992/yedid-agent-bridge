# Yedid Smart Hub v2.0 (`yedid-agent-bridge`)

**Yedid Smart Hub** es una plataforma local de asistencia inteligente, domótica privada, memoria cifrada y **orquestación multiagente determinista** diseñada para ejecutarse 100% en hardware local bajo control humano estricto (**Human-in-the-Loop / HITL**), con interfaz progresiva (**PWA v9**) optimizada para **Samsung Galaxy Z Fold 7** y escritorio.

Este repositorio (`jaimezabala1992/yedid-agent-bridge`) actúa como el **puente de código, arquitectura y colaboración técnica** entre:
- **Gemini Antigravity (Ejecutor Local):** Desarrollo, ejecución supervisada en el entorno local (`C:\Yedid-Smart-Home`), pruebas de integración, control de procesos y validación en vivo.
- **ChatGPT / Yedid (Arquitecto y Revisor en GitHub):** Consulta de código fuente mediante el conector oficial de GitHub, revisión de arquitectura, detección de errores, diseño de mejoras y planificación conjunta.
- **GitHub (`main`):** Fuente de verdad compartida de código fuente sanitizado y documentación técnica.

---

## 🏗️ Vista Rápida del Ecosistema

| Servicio / Componente | Puerto / Ubicación | Rol Principal |
| :--- | :--- | :--- |
| **Yedid Core Hub (`hub.server:app`)** | `127.0.0.1:8765` | API FastAPI, Enrutador LLM, Motor Duradero de Tareas (`TaskEngine`), Historial SQLite (`ChatHistoryDB`), Memoria v2.2 y PWA v9. |
| **Yedid MCP Gateway (`mcp_gateway.server:gateway_app`)** | `127.0.0.1:8766` | Pasarela Model Context Protocol (FastMCP) con aislamiento de archivos y control de dispositivos. |
| **Ollama Local Runtime (`qwen3:8b`)** | `127.0.0.1:11434` | Inferencia conversacional local en CPU y ruteo nativo de herramientas JSON Schema (manteniendo la GPU libre para Sunshine/Gaming). |
| **Gemini CLI / Antigravity (`agy.exe`)** | Subproceso supervisado | Agente local de ingeniería de software, diagnóstico y continuación de tareas sin límites de cuota. |
| **Codex Runtime Inspector** | Telemetría SQLite/JSONL | Inspección real en disco de sesiones Codex, detección de agotamiento de cuota (`usage_limit_exceeded`) y traspaso automático (*handover*) hacia Gemini CLI. |
| **Yedid Agent Bridge (`agent_bridge`)** | Demonio asíncrono | Puente seguro de tareas estructuradas vía GitHub Issues / API REST con validación de esquemas y políticas de seguridad. |

---

## 📂 Estructura del Repositorio

```text
yedid-agent-bridge/
├── README.md                  # Descripción general y guía de inicio rápido
├── SECURITY.md                # Política de seguridad, zonas prohibidas y modelo HITL
├── AGENTS.md                  # Reglas operativas para agentes locales y remotos
├── .env.example               # Plantilla sanitizada de variables de entorno
├── .gitignore                 # Exclusión estricta de bases de datos, tokens y secretos
├── requirements.txt           # Dependencias de Python 3.12+
├── docs/
│   ├── architecture.md        # Arquitectura detallada del sistema y flujos de datos
│   ├── components.md          # Catálogo técnico de módulos, clases y endpoints
│   ├── development.md         # Guía de desarrollo y protocolo de colaboración Gemini <-> ChatGPT
│   └── current-status.md      # Estado actual (v2.0), correcciones recientes y hoja de ruta
├── hub/                       # Núcleo de Yedid Smart Hub (FastAPI + Motores SQLite + PWA)
│   ├── server.py              # Endpoints HTTP, autenticación, CSRF y ciclo de vida
│   ├── llm_router.py          # Enrutador LLM multi-turno, intenciones multiagente y síntesis determinista
│   ├── task_engine.py         # Motor duradero de tareas asíncronas en SQLite (agent_tasks.db)
│   ├── chat_history_db.py     # Persistencia transaccional de conversaciones y mensajes (chat_history.db)
│   ├── agent_inspector.py     # Telemetría real en disco de Codex, Gemini CLI y traspaso de tareas
│   ├── agent_adapters.py      # Adaptadores supervisados de ejecución (GeminiCLIAdapter, SafeScriptAdapter)
│   ├── approvals.py           # Gestor Human-in-the-Loop (HITL) con hash criptográfico SHA-256 de alcance
│   ├── security_policy.py     # Listas blancas de proyectos, zonas rojas prohibidas y clasificación de riesgo
│   ├── process_guard.py       # Aislamiento en kernel de Windows mediante Job Objects
│   ├── memory_engine.py       # Motor de memoria personal v2.2 con control de acceso y propuestas
│   ├── memory_db.py           # Almacenamiento SQLite FTS5 + cifrado autenticado por columna
│   ├── memory_crypto.py       # Cifrado AES-256-GCM / Fernet protegido con Windows DPAPI
│   └── static/                # Interfaz PWA v9 (index.html, sw.js, manifest.json, iconos)
├── mcp_gateway/               # Pasarela Model Context Protocol (puerto 8766)
├── agent_bridge/              # Puente asíncrono de tareas con GitHub API
└── scripts/                   # Utilidades de administración, autoinicio y sincronización GitHub
```

---

## 🔒 Garantías de Seguridad en este Repositorio

1. **Cero Datos Personales o Secretos:** Este repositorio contiene exclusivamente código fuente y documentación sanitizada. Ninguna base de datos SQLite (`*.db`), token (`.token`, `.github_token`), certificado TLS/ADB (`*.pem`, `*.key`), historial de chat ni memoria personal se incluye en el control de versiones.
2. **Aislamiento de Instalación Funcional:** El entorno productivo en `C:\Yedid-Smart-Home` opera de manera independiente; la copia de sincronización con GitHub reside en un espacio de trabajo aislado (`%USERPROFILE%\workspace\yedid-agent-bridge`) validado por un escáner pre-push.
3. **Human-in-the-Loop (HITL) Criptográfico:** Toda tarea de riesgo `HIGH` (ejecución o edición en directorios de proyecto fuera del sandbox) genera una solicitud bloqueante vinculada mediante hash SHA-256 canónico a la **Tarea (`task_id`)**, **Alcance (`project_path`)** y **Riesgo (`risk_level`)**.

---

## 🤝 Flujo de Colaboración Gemini Antigravity ↔ ChatGPT (Yedid)

Consulta [`docs/development.md`](docs/development.md) para el protocolo completo de ramas, commits y Pull Requests:
1. **Inspección y Diagnóstico (ChatGPT):** ChatGPT analiza el código en la rama `main` (y el estado reportado en [`docs/current-status.md`](docs/current-status.md)), propone mejoras arquitectónicas o parches concretos haciendo referencia a rutas y líneas exactas.
2. **Implementación y Pruebas Locales (Gemini Antigravity):** Gemini aplica los cambios en el entorno local, ejecuta las suites de pruebas de aceptación y seguridad contra los servicios reales y actualiza la copia sanitizada del repositorio.
3. **Sincronización Auditada (GitHub):** Tras ejecutar el verificador de seguridad (`scripts/github_sync.py --audit`) y contar con autorización del propietario, se publican los commits hacia `jaimezabala1992/yedid-agent-bridge` para la siguiente iteración con ChatGPT.
