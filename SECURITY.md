# SECURITY.md — Políticas de Seguridad, Permisos y Secretos (Yedid Smart Home)

## 1. Principio de Cero Confianza y Privilegio Mínimo

1. **Sin Shell Remoto:** Ningún modelo de lenguaje (sea local o en la nube) tiene acceso a comandos de PowerShell, CMD, Bash o llamadas arbitrarias a `subprocess`. Todas las acciones pasan por herramientas con esquema formal y validación estricta de parámetros.
2. **Autoridad Central del Hub:** Los LLM sugieren intenciones; el Hub y Home Assistant deciden si la acción es admisible según el catálogo y las políticas de seguridad.
3. **Loopback por Defecto:** Los servicios internos (`Ollama` en `11434`, `Yedid OS` en `8765`) operan exclusivamente en `127.0.0.1`. No se exponen puertos a la red pública de Internet.
4. **Acceso Remoto Vía Tailscale:** Cualquier consulta o control desde fuera de la residencia debe canalizarse a través de la red privada autenticada de Tailscale (`100.78.107.54`), nunca abriendo puertos en el router hacia la WAN.

## 2. Gestión de Credenciales y Secretos

- **Prohibido Guardar Claves en Repositorios o Chats:** Ni tokens OAuth, ni claves de OpenAI, ni credenciales de Home Assistant se guardarán en texto plano en archivos versionados.
- **Archivos de Traspaso Sanitizados:** `YEDID_HANDOFF.md` y `AGENT_INVENTORY.md` deben contener exclusivamente identificadores redactados o simbólicos.
- **Secretos en Almacén Local:** Variables de entorno seguras (`.env` ignorado por `.gitignore`) o el almacén de credenciales del sistema operativo.

## 3. Clasificación de Dispositivos y Niveles de Riesgo

| Categoría | Ejemplos | Nivel de Riesgo | Política de Ejecución |
|---|---|---|---|
| **Lectura / Estado** | "¿Está encendido el Google TV?", "¿Cuánta energía solar hay?" | Muy Bajo | Ejecución autónoma sin confirmación. |
| **Multimedia y Ocio** | `play`, `pause`, volumen, cambiar canal, lanzar Moonlight | Bajo | Ejecución directa permitida por agentes autorizados. |
| **Escenas Ambientales** | "Modo Gaming", "Modo Película" | Medio | Ejecución permitida si los dispositivos están preaprobados. |
| **Infraestructura Eléctrica** | Tesla Powerwall (Gateway), paneles solares | Alto | **Solo lectura.** Prohibido alterar reservas o configuración. |
| **Seguridad Física** | Cerraduras, portones, alarmas, cámaras | Crítico | **Bloqueo absoluto.** Requiere autorización humana explícita. |

## 4. Plan de Rollback y Recuperación

- Si cualquier servicio presenta fallas de memoria o interfiere con el uso recreativo del equipo (Ryzen 5 5600X / 16 GB RAM), los procesos de agentes pueden detenerse inmediatamente sin afectar el arranque de Windows ni la conectividad de Sunshine.
- Copias de seguridad periódicas de las configuraciones de Home Assistant y de la base de datos de tareas SQLite.
