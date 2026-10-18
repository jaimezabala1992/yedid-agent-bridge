"""
tests/test_chatgpt_audit_fixes.py

Suite de pruebas automatizadas en entorno 100% aislado para verificar:
1. Aislamiento estricto de bases de datos de producción (`YEDID_TEST_MODE=1` y `assert_not_production_db_in_test`).
2. Consultas SQL parametrizadas sin `os.getenv` literal en `hub/chat_history_db.py`.
3. Protección XSS estática y delegación de eventos (`addEventListener` / `data-action`) sin `onclick` dinámicos en `hub/static/index.html`.
4. Prueba real en navegador Chromium headless (`--headless --dump-dom`) ejecutando el DOM y JavaScript real de `index.html` contra payloads XSS maliciosos.
5. Prevención de inyección de código en `SafeScriptAdapter.execute_task` (`hub/agent_adapters.py`).
6. Recuperación cronológica de los mensajes más recientes en `get_recent_context_for_llm()`.
7. Mecanismo transaccional de idempotencia (`processing`, `completed`, `failed`) y vinculación explícita solicitud-respuesta (`reply_to_message_id`, `request_client_message_id`).
8. Solicitudes concurrentes (`asyncio.gather`), reintentos de red y garantía de ejecución única de herramientas (`execute_tool`).
9. Regresión de turnos concurrentes entrelazados en una misma conversación (vinculación explícita inmune al orden de llegada).
10. Manejo idempotente de solicitudes fallidas (`status='failed'`) sin re-ejecución accidental.
11. Prevención de condición de carrera entre cancelación (`cancel_task`) y finalización en `hub/task_engine.py`.
12. Protección contra eliminación accidental de archivos remotos en `scripts/github_sync.py` (`inspect_remote_tree_diff` y `build_tree_payload`).
"""

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

# Activar modo de aislamiento de pruebas ANTES de importar cualquier módulo del Hub
_GLOBAL_TEST_DATA_DIR = tempfile.mkdtemp(prefix="yedid_isolated_suite_")
os.environ["YEDID_TEST_MODE"] = "1"
os.environ["YEDID_DATA_DIR"] = _GLOBAL_TEST_DATA_DIR
os.environ.setdefault("YEDID_TOKEN", "test_isolated_token_ci_only")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hub.audit as hub_audit
from hub.config import assert_not_production_db_in_test
from hub.chat_history_db import ChatHistoryDB, chat_history_db
from hub.agent_adapters import SafeScriptAdapter
from hub.task_engine import TaskEngine, task_engine
from hub.llm_router import LLMRouter
from scripts.github_sync import inspect_remote_tree_diff, build_tree_payload


def _snapshot_production_dbs() -> Dict[str, Optional[str]]:
    """Toma huella SHA-256 de las bases de datos de producción (si existen) para verificar que jamás se toquen."""
    prod_dir = Path("C:/Yedid-Smart-Home/data")
    snapshots: Dict[str, Optional[str]] = {}
    if not prod_dir.exists():
        return snapshots
    for name in ("chat_history.db", "agent_tasks.db", "memories_v2.db", "memory.db"):
        p = prod_dir / name
        if p.exists():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            snapshots[name] = h
        else:
            snapshots[name] = None
    return snapshots


def _find_headless_browser() -> Optional[str]:
    """Localiza un navegador Chromium (Edge o Chrome) disponible en el sistema para pruebas reales de DOM."""
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


class TestChatGPTAuditFixes(unittest.IsolatedAsyncioTestCase):
    """Pruebas aisladas para los hallazgos de la primera y segunda revisión de ChatGPT (Yedid)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._prod_snapshot_before = _snapshot_production_dbs()

    @classmethod
    def tearDownClass(cls) -> None:
        prod_snapshot_after = _snapshot_production_dbs()
        assert cls._prod_snapshot_before == prod_snapshot_after, (
            f"¡VIOLACIÓN DE AISLAMIENTO! Las bases de datos de producción cambiaron durante las pruebas: "
            f"antes={cls._prod_snapshot_before} vs después={prod_snapshot_after}"
        )
        shutil.rmtree(_GLOBAL_TEST_DATA_DIR, ignore_errors=True)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="yedid_audit_test_")
        self.tmp_path = Path(self.temp_dir.name)
        os.environ["YEDID_TEST_MODE"] = "1"
        os.environ["YEDID_DATA_DIR"] = str(self.tmp_path)
        hub_audit.AUDIT_LOG_FILE = self.tmp_path / "audit.jsonl"

        self.chat_db_path = self.tmp_path / "test_chat_history.db"
        self.tasks_db_path = self.tmp_path / "test_agent_tasks.db"

        self.db = ChatHistoryDB(db_path=self.chat_db_path)
        self.engine = TaskEngine(db_path=self.tasks_db_path)

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_production_db_isolation_guard(self) -> None:
        """
        Punto 3: Verificar que las pruebas automatizadas jamás inicialicen ni modifiquen
        bases de datos en `C:\\Yedid-Smart-Home\\data` ni en el directorio `data/` del repositorio.
        """
        with self.assertRaises(RuntimeError) as ctx_prod:
            assert_not_production_db_in_test(Path("C:/Yedid-Smart-Home/data/chat_history.db"))
        self.assertIn("TEST ISOLATION GUARD", str(ctx_prod.exception))

        with self.assertRaises(RuntimeError) as ctx_repo:
            assert_not_production_db_in_test(REPO_ROOT / "data" / "chat_history.db")
        self.assertIn("TEST ISOLATION GUARD", str(ctx_repo.exception))

        with self.assertRaises(RuntimeError):
            ChatHistoryDB(db_path=Path("C:/Yedid-Smart-Home/data/chat_history.db"))

        with self.assertRaises(RuntimeError):
            TaskEngine(db_path=Path("C:/Yedid-Smart-Home/data/agent_tasks.db"))

    def test_02_parameterized_sql_queries_in_chat_history_db(self) -> None:
        """
        Hallazgo #1: Verificar que ninguna sentencia SQL contenga `os.getenv` literal
        y que `list_conversations`, `get_conversation` y `delete_conversation` funcionen con `?`.
        """
        source_path = REPO_ROOT / "hub" / "chat_history_db.py"
        source_text = source_path.read_text(encoding="utf-8")

        tree = ast.parse(source_text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val_upper = node.value.upper()
                if "SELECT " in val_upper or "DELETE " in val_upper or "UPDATE " in val_upper:
                    self.assertNotIn(
                        "os.getenv",
                        node.value,
                        f"Sentencia SQL contiene 'os.getenv' literal: {node.value}",
                    )

        with patch.dict(os.environ, {"YEDID_OWNER_EMAIL": "custom_owner@domain.org"}):
            conv = self.db.create_conversation(user_id="custom_owner@domain.org", title="Prueba SQL Parametrizado")
            cid = conv["id"]
            self.db.add_message(conversation_id=cid, sender="user", content="Hola mundo")

            listed = self.db.list_conversations(user="jaime", limit=10)
            self.assertTrue(any(c["id"] == cid for c in listed))

            fetched = self.db.get_conversation(cid, user="jaime")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched["message_count"], 1)

            deleted = self.db.delete_conversation(cid, user="jaime")
            self.assertTrue(deleted)
            self.assertIsNone(self.db.get_conversation(cid))

    def test_03_xss_static_and_event_delegation_audit(self) -> None:
        """
        Hallazgo #2 y Punto 2: Verificar que `hub/static/index.html` elimine todos los manejadores
        `onclick`/`onchange` con interpolación `${...}`, utilice `addEventListener()` con delegación
        de eventos (`data-action`) y escape el contenido HTML.
        """
        index_path = REPO_ROOT / "hub" / "static" / "index.html"
        html = index_path.read_text(encoding="utf-8")

        self.assertIn("function escapeHtml(", html)
        self.assertIn("safeText = escapeHtml(safeText)", html)
        self.assertIn("function initDynamicEventDelegation()", html)
        self.assertIn("document.addEventListener('click'", html)
        self.assertIn("document.addEventListener('change'", html)

        # Ningún atributo on* en todo el documento puede interpolar variables dinámicas ${...}
        dynamic_inline_handlers = re.findall(
            r'\bon[a-z]+\s*=\s*["\'][^"\'\r\n]*\$\{[^"\'\r\n]*["\']',
            html,
            flags=re.IGNORECASE,
        )
        self.assertEqual(
            dynamic_inline_handlers,
            [],
            f"Se encontraron manejadores inline con interpolación dinámica: {dynamic_inline_handlers}",
        )

        # Verificar que las funciones de renderizado dinámico usen data-action en lugar de onclick
        self.assertIn('data-action="resolve-approval"', html)
        self.assertIn('data-action="resume-task"', html)
        self.assertIn('data-action="review-single-memory"', html)
        self.assertIn('data-action="view-memory-detail"', html)
        self.assertIn('data-action="open-edit-memory"', html)
        self.assertIn('data-action="delete-memory"', html)
        self.assertIn('data-action="review-memory-chat"', html)

    def test_04_real_browser_dom_xss_and_event_delegation(self) -> None:
        """
        Punto 2: Prueba real en navegador Chromium headless ejecutando las funciones reales de
        `hub/static/index.html` (`escapeHtml`, `formatChatTextHtml`, `renderTaskCardHtml`,
        `renderApprovalsModal`, `appendMessage`, `initDynamicEventDelegation`) contra payloads
        maliciosos de ruptura de atributos y scripts, verificando que:
        1) Ningún payload XSS se ejecute en el DOM real (`window.__xss_count === 0`).
        2) Los clics reales sobre botones delegados (`[data-action]`) pasen los identificadores
           literales sin interpretar código JavaScript.
        """
        browser_bin = _find_headless_browser()
        if not browser_bin:
            self.skipTest("No se encontró navegador Chromium headless en el entorno.")

        index_html = (REPO_ROOT / "hub" / "static" / "index.html").read_text(encoding="utf-8")

        def extract_js_function(source: str, func_sig: str) -> str:
            start = source.index(func_sig)
            brace_idx = source.index("{", start)
            depth = 0
            for idx in range(brace_idx, len(source)):
                ch = source[idx]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return source[start : idx + 1]
            raise ValueError(f"No se pudo extraer {func_sig}")

        js_escape = extract_js_function(index_html, "function escapeHtml(")
        js_format = extract_js_function(index_html, "function formatChatTextHtml(")
        js_task_card = extract_js_function(index_html, "function renderTaskCardHtml(")
        js_approvals = extract_js_function(index_html, "function renderApprovalsModal(")
        js_append = extract_js_function(index_html, "function appendMessage(")
        js_delegation = extract_js_function(index_html, "function initDynamicEventDelegation(")

        harness_file = self.tmp_path / "browser_xss_harness.html"
        harness_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>XSS Audit Harness</title></head>
<body>
  <div id="approvals-list"></div>
  <div id="chat-messages"></div>
  <pre id="audit-result">RUNNING</pre>
  <script>
    window.__xss_count = 0;
    window.__xss = function() {{ window.__xss_count++; }};
    window.__captured_events = [];

    function resolveApproval(id, decision) {{
      window.__captured_events.push({{ type: 'resolveApproval', id, decision }});
    }}
    function resumeTask(taskId) {{
      window.__captured_events.push({{ type: 'resumeTask', taskId }});
    }}
    function reviewSingleMemory(memoryId, decision) {{
      window.__captured_events.push({{ type: 'reviewSingleMemory', memoryId, decision }});
    }}
    function viewMemoryDetail(memoryId) {{
      window.__captured_events.push({{ type: 'viewMemoryDetail', memoryId }});
    }}
    function openEditMemoryModal(memoryId) {{
      window.__captured_events.push({{ type: 'openEditMemoryModal', memoryId }});
    }}
    function deleteMemory(memoryId) {{
      window.__captured_events.push({{ type: 'deleteMemory', memoryId }});
    }}
    function openStepupModal() {{
      window.__captured_events.push({{ type: 'openStepupModal' }});
    }}
    function reviewMemoryFromChat(memoryId, decision, el) {{
      window.__captured_events.push({{ type: 'reviewMemoryFromChat', memoryId, decision }});
    }}
    function updateImportPreviewCount() {{
      window.__captured_events.push({{ type: 'updateImportPreviewCount' }});
    }}

    {js_escape}
    {js_format}
    {js_task_card}
    {js_approvals}
    {js_append}
    {js_delegation}

    initDynamicEventDelegation();

    // 1. Renderizar modal de aprobaciones con payloads maliciosos en IDs y campos
    const maliciousApprovalId = "appr_1'); window.__xss(); //\\"><img src=x onerror=window.__xss()>";
    const maliciousTaskId = "task_1'); window.__xss(); //\\"><svg onload=window.__xss()>";
    renderApprovalsModal([{{
      id: maliciousApprovalId,
      task_id: maliciousTaskId,
      agent: "<script>window.__xss()<\\/script>",
      scope: "C:\\\\sandbox\\"><img src=x onerror=window.__xss()>",
      risk_label: "HIGH\\"><svg onload=window.__xss()>",
      instruction: "Revisar '); window.__xss(); //"
    }}]);

    // 2. Renderizar mensaje de chat con herramientas, propuesta de memoria y tarjeta de tarea maliciosa
    const maliciousMemId = "mem_1'); window.__xss(); //\\"><script>window.__xss()<\\/script>";
    appendMessage(
      "assistant",
      "Hola **<img src=x onerror=window.__xss()>** `<script>window.__xss()<\\/script>`",
      [
        {{
          tool: "propose_memory_to_save",
          result: {{
            id: maliciousMemId,
            title: "<img src=x onerror=window.__xss()>",
            category: "general'); window.__xss(); //",
            access_level: "personal",
            content: "<svg onload=window.__xss()>"
          }}
        }}
      ],
      maliciousTaskId,
      {{
        id: maliciousTaskId,
        status: "failed",
        approval_id: maliciousApprovalId,
        instruction: "<iframe src=javascript:window.__xss()></iframe>",
        project_path: "C:\\\\sandbox'); window.__xss(); //",
        agent: "safe_script\\"><img src=x onerror=window.__xss()>",
        risk_level: "HIGH",
        deliverables: [{{ name: "<img src=x onerror=window.__xss()>.txt", size_bytes: "42\\" onload=window.__xss()" }}]
      }},
      new Date().toISOString()
    );

    // 3. Disparar clics reales en todos los botones con data-action en el DOM del navegador
    document.querySelectorAll('[data-action]').forEach(btn => btn.click());

    const activeImgs = document.querySelectorAll('#chat-messages img, #approvals-list img, #chat-messages script, #approvals-list script, #chat-messages svg, #chat-messages iframe');
    document.getElementById('audit-result').textContent = JSON.stringify({{
      xss_count: window.__xss_count,
      injected_elements_count: activeImgs.length,
      captured_events: window.__captured_events
    }});
  </script>
</body>
</html>"""
        harness_file.write_text(harness_html, encoding="utf-8")

        user_data_dir = self.tmp_path / "chromium_profile"
        cmd = [
            browser_bin,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={user_data_dir}",
            "--dump-dom",
            harness_file.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
        self.assertEqual(proc.returncode, 0, f"Fallo ejecutando navegador headless: {proc.stderr}")

        m = re.search(r'<pre id="audit-result">(.*?)</pre>', proc.stdout, flags=re.DOTALL)
        self.assertIsNotNone(m, f"No se encontró #audit-result en el DOM volcado:\n{proc.stdout[:1000]}")
        import html as html_mod
        raw_json = html_mod.unescape(m.group(1).strip())
        result = json.loads(raw_json)

        self.assertEqual(result["xss_count"], 0, f"¡Se ejecutó código XSS en el navegador!: {result}")
        self.assertEqual(result["injected_elements_count"], 0, "Se inyectaron nodos HTML peligrosos en el DOM.")
        self.assertEqual(len(result["captured_events"]), 5)

        event_types = [e["type"] for e in result["captured_events"]]
        self.assertIn("resolveApproval", event_types)
        self.assertIn("resumeTask", event_types)
        self.assertIn("reviewMemoryFromChat", event_types)

        # Verificar que el ID malicioso llegó íntegro como dato inerte al manejador delegado
        approval_events = [e for e in result["captured_events"] if e["type"] == "resolveApproval"]
        self.assertTrue(all("window.__xss()" in e["id"] for e in approval_events))

    async def test_05_safe_script_adapter_prevents_code_injection(self) -> None:
        """
        Hallazgo #3: Verificar que `SafeScriptAdapter.execute_task` pase instrucciones
        únicamente por variables de entorno inertes y jamás las interpole en el código fuente Python.
        """
        adapter = SafeScriptAdapter()
        malicious_instruction = (
            'diagnostico """ + __import__("os").system("echo INJECTED") + """ '
            "'; import os; os.system('echo PWNED'); #"
        )

        with patch("hub.agent_adapters.validate_project_path", return_value=(True, self.tmp_path, "OK")), \
             patch("hub.agent_adapters.is_sensitive_path", return_value=False):
            res = await adapter.execute_task(
                instruction=malicious_instruction,
                project_path=self.tmp_path,
                timeout_seconds=15,
            )

        self.assertTrue(res["success"], f"El script seguro debió completarse con éxito: {res}")
        self.assertEqual(res["returncode"], 0)

        generated_scripts = list(self.tmp_path.glob("task_exec_*.py"))
        self.assertEqual(len(generated_scripts), 1)
        script_code = generated_scripts[0].read_text(encoding="utf-8")

        self.assertNotIn("INJECTED", script_code)
        self.assertNotIn("PWNED", script_code)
        self.assertIn('os.environ.get("YEDID_TASK_INSTRUCTION"', script_code)

        deliverables = list(self.tmp_path.glob("resultado_tarea_*.txt"))
        self.assertEqual(len(deliverables), 1)
        report_content = deliverables[0].read_text(encoding="utf-8")
        self.assertIn(malicious_instruction, report_content)

    def test_06_get_recent_context_for_llm_returns_newest_messages(self) -> None:
        """
        Hallazgo #4: Cuando una conversación supera `limit * 2` mensajes,
        `get_recent_context_for_llm(limit=3)` debe devolver los últimos 6 mensajes más recientes
        en orden cronológico ascendente.
        """
        conv = self.db.create_conversation(title="Context Window Test")
        cid = conv["id"]

        for i in range(1, 11):
            self.db.add_message(conversation_id=cid, sender="user", content=f"Pregunta usuario {i}")
            self.db.add_message(conversation_id=cid, sender="assistant", content=f"Respuesta asistente {i}")

        recent = self.db.get_recent_context_for_llm(cid, limit=3)
        self.assertEqual(len(recent), 6)

        expected_contents = [
            "Pregunta usuario 8",
            "Respuesta asistente 8",
            "Pregunta usuario 9",
            "Respuesta asistente 9",
            "Pregunta usuario 10",
            "Respuesta asistente 10",
        ]
        actual_contents = [m["content"] for m in recent]
        self.assertEqual(actual_contents, expected_contents)

    def test_07_transactional_idempotency_lifecycle_and_explicit_linkage(self) -> None:
        """
        Punto 1: Verificar la máquina de estados transaccional (`processing` -> `completed` / `failed`)
        en `chat_requests` y la vinculación explícita `reply_to_message_id` / `request_client_message_id`.
        """
        conv = self.db.create_conversation(title="Idempotency Lifecycle")
        cid = conv["id"]

        # 1. Reclamar solicitud nueva -> estado 'processing'
        claim1 = self.db.begin_idempotent_request(
            conversation_id=cid,
            client_message_id="cmsg_tx_1",
            user_content="Ejecuta diagnóstico",
        )
        self.assertTrue(claim1["claimed"])
        self.assertEqual(claim1["status"], "processing")
        self.assertIsNotNone(claim1["user_message_id"])

        # 2. Reclamo duplicado mientras sigue en 'processing' -> claimed=False, status='processing'
        claim2 = self.db.begin_idempotent_request(
            conversation_id=cid,
            client_message_id="cmsg_tx_1",
            user_content="Ejecuta diagnóstico",
        )
        self.assertFalse(claim2["claimed"])
        self.assertEqual(claim2["status"], "processing")
        self.assertEqual(claim2["user_message_id"], claim1["user_message_id"])

        # Verificar que solo exista 1 mensaje de usuario en SQLite
        self.assertEqual(len(self.db.get_messages(cid)), 1)

        # 3. Completar transaccionalmente la solicitud
        asst_msg = self.db.complete_idempotent_request(
            conversation_id=cid,
            user_message_id=claim1["user_message_id"],
            content="Diagnóstico finalizado con éxito.",
            tools_called=[{"tool": "pc_get_status", "result": {"cpu_percent": 15}}],
            client_message_id="cmsg_tx_1",
        )
        self.assertEqual(asst_msg["reply_to_message_id"], claim1["user_message_id"])
        self.assertEqual(asst_msg["request_client_message_id"], "cmsg_tx_1")

        status_after = self.db.get_request_status("cmsg_tx_1", conversation_id=cid)
        self.assertIsNotNone(status_after)
        self.assertEqual(status_after["status"], "completed")
        self.assertEqual(status_after["response_message_id"], asst_msg["message_id"])
        self.assertEqual(status_after["assistant_message"]["content"], "Diagnóstico finalizado con éxito.")

    async def test_08_concurrent_requests_and_single_tool_execution(self) -> None:
        """
        Punto 1: Cuando llegan múltiples solicitudes concurrentes (`asyncio.gather`) con el mismo
        `client_message_id` mientras la primera sigue en procesamiento (y posteriores reintentos de red):
        - El LLM y las herramientas (`execute_tool`) se ejecutan EXACTAMENTE UNA VEZ.
        - Todas las solicitudes concurrentes reciben la misma respuesta completada y tarea vinculada.
        - No se duplican mensajes de usuario ni de asistente en SQLite.
        """
        conv = self.db.create_conversation(title="Concurrent Idempotency Test")
        cid = conv["id"]

        tool_execution_count = 0
        llm_call_count = 0

        async def fake_execute_tool(tool_name, args, conversation_id=None):
            nonlocal tool_execution_count
            tool_execution_count += 1
            await asyncio.sleep(0.08)
            return {
                "tool": tool_name,
                "result": {"cpu_percent": 22, "ram_percent": 40, "ram_used_gb": 12, "ram_total_gb": 32, "disk_free_gb": 500},
            }

        class FakeResponse:
            def __init__(self, json_data):
                self._data = json_data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        async def fake_post(url, json=None, **kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            await asyncio.sleep(0.05)
            if llm_call_count == 1:
                return FakeResponse({
                    "message": {
                        "content": "",
                        "tool_calls": [{"function": {"name": "pc_get_status", "arguments": {}}}],
                    }
                })
            return FakeResponse({
                "message": {
                    "content": "Estado del PC verificado: CPU al 22% y RAM al 40%.",
                }
            })

        router = LLMRouter()
        with patch("hub.llm_router.chat_history_db", self.db), \
             patch("hub.llm_router.task_engine", self.engine), \
             patch("hub.llm_router.execute_tool", side_effect=fake_execute_tool), \
             patch("httpx.AsyncClient.post", side_effect=fake_post):

            # Lanzar 8 solicitudes concurrentes idénticas con el mismo client_message_id
            tasks = [
                router.chat(
                    user_message="¿Cómo está el estado del PC?",
                    conversation_id=cid,
                    client_message_id="cmsg_concurrent_001",
                )
                for _ in range(8)
            ]
            results = await asyncio.gather(*tasks)

            # Verificar que la herramienta se ejecutó EXACTAMENTE 1 vez
            self.assertEqual(tool_execution_count, 1, f"La herramienta debió ejecutarse 1 sola vez, se ejecutó {tool_execution_count}")
            self.assertEqual(llm_call_count, 2, "Solo debieron realizarse las 2 llamadas al LLM del turno original")

            # Exactamente 1 solicitud fue la procesadora original (deduplicated=False) y 7 fueron deduplicadas (deduplicated=True)
            originals = [r for r in results if not r.get("deduplicated")]
            deduped = [r for r in results if r.get("deduplicated")]
            self.assertEqual(len(originals), 1)
            self.assertEqual(len(deduped), 7)

            for r in results:
                self.assertEqual(r["reply"], "Estado del PC verificado: CPU al 22% y RAM al 40%.")
                self.assertEqual(r["request_status"], "completed")
                self.assertEqual(len(r["tools_executed"]), 1)
                self.assertEqual(r["tools_executed"][0]["tool"], "pc_get_status")

            # Reintento de red posterior tras finalizar
            retry_res = await router.chat(
                user_message="¿Cómo está el estado del PC?",
                conversation_id=cid,
                client_message_id="cmsg_concurrent_001",
            )
            self.assertTrue(retry_res["deduplicated"])
            self.assertEqual(retry_res["reply"], "Estado del PC verificado: CPU al 22% y RAM al 40%.")
            self.assertEqual(tool_execution_count, 1)

            # Verificar que en SQLite solo existan exactamente 2 mensajes (1 user + 1 assistant) vinculados explícitamente
            all_msgs = self.db.get_messages(cid)
            self.assertEqual(len(all_msgs), 2)
            self.assertEqual(all_msgs[0]["sender"], "user")
            self.assertEqual(all_msgs[1]["sender"], "assistant")
            self.assertEqual(all_msgs[1]["reply_to_message_id"], all_msgs[0]["message_id"])
            self.assertEqual(all_msgs[1]["request_client_message_id"], "cmsg_concurrent_001")

    async def test_09_interleaved_concurrent_conversations_explicit_linkage_regression(self) -> None:
        """
        Punto 1 y Punto 3 (Regresión de concurrencia):
        Si dos solicitudes DISTINTAS (`cmsg_slow_A` y `cmsg_fast_B`) llegan concurrentemente a la
        MISMA conversación y `cmsg_fast_B` termina ANTES que `cmsg_slow_A`, el vínculo explícito
        (`reply_to_message_id` / `response_message_id`) garantiza que cada `client_message_id`
        recupere exactamente su propia respuesta sin cruzarse jamás.
        """
        conv = self.db.create_conversation(title="Interleaved Turn Linkage Test")
        cid = conv["id"]

        class FakeResponse:
            def __init__(self, text):
                self._text = text

            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": self._text}}

        async def fake_post(url, json=None, **kwargs):
            user_prompt = json["messages"][-1]["content"]
            if "LENTA_A" in user_prompt:
                await asyncio.sleep(0.12)
                return FakeResponse("Respuesta exclusiva para solicitud LENTA_A.")
            else:
                await asyncio.sleep(0.01)
                return FakeResponse("Respuesta exclusiva para solicitud RAPIDA_B.")

        router = LLMRouter()
        with patch("hub.llm_router.chat_history_db", self.db), \
             patch("hub.llm_router.task_engine", self.engine), \
             patch("httpx.AsyncClient.post", side_effect=fake_post):

            res_a, res_b = await asyncio.gather(
                router.chat(user_message="Consulta LENTA_A", conversation_id=cid, client_message_id="cmsg_slow_A"),
                router.chat(user_message="Consulta RAPIDA_B", conversation_id=cid, client_message_id="cmsg_fast_B"),
            )

            self.assertEqual(res_a["reply"], "Respuesta exclusiva para solicitud LENTA_A.")
            self.assertEqual(res_b["reply"], "Respuesta exclusiva para solicitud RAPIDA_B.")

            # Reintentar ambas solicitudes y verificar que el vínculo explícito devuelva la respuesta exacta de cada una
            retry_a = await router.chat(user_message="Consulta LENTA_A", conversation_id=cid, client_message_id="cmsg_slow_A")
            retry_b = await router.chat(user_message="Consulta RAPIDA_B", conversation_id=cid, client_message_id="cmsg_fast_B")

            self.assertTrue(retry_a["deduplicated"])
            self.assertEqual(retry_a["reply"], "Respuesta exclusiva para solicitud LENTA_A.")
            self.assertEqual(retry_a["request_message_id"], res_a["request_message_id"])
            self.assertEqual(retry_a["response_message_id"], res_a["response_message_id"])

            self.assertTrue(retry_b["deduplicated"])
            self.assertEqual(retry_b["reply"], "Respuesta exclusiva para solicitud RAPIDA_B.")
            self.assertEqual(retry_b["request_message_id"], res_b["request_message_id"])
            self.assertEqual(retry_b["response_message_id"], res_b["response_message_id"])

    async def test_10_failed_request_idempotency_no_duplicate_tool_execution(self) -> None:
        """
        Punto 1: Cuando una solicitud falla (ej. Ollama inalcanzable sin herramientas pre-ejecutadas),
        queda registrada transaccionalmente con `status='failed'` en `chat_requests` y su mensaje de
        error vinculado explícitamente, evitando reprocesamientos duplicados en reintentos con el mismo ID.
        """
        conv = self.db.create_conversation(title="Failed Request State Test")
        cid = conv["id"]
        ollama_attempts = 0

        async def failing_post(url, json=None, **kwargs):
            nonlocal ollama_attempts
            ollama_attempts += 1
            raise ConnectionError("Ollama local connection refused")

        router = LLMRouter()
        with patch("hub.llm_router.chat_history_db", self.db), \
             patch("hub.llm_router.task_engine", self.engine), \
             patch("httpx.AsyncClient.post", side_effect=failing_post):

            first_res = await router.chat(
                user_message="Hola Yedid",
                conversation_id=cid,
                client_message_id="cmsg_fail_01",
            )
            self.assertFalse(first_res["deduplicated"])
            self.assertEqual(first_res["request_status"], "failed")
            self.assertIn("Ollama", first_res["reply"])
            self.assertEqual(ollama_attempts, 1)

            req_state = self.db.get_request_status("cmsg_fail_01", conversation_id=cid)
            self.assertIsNotNone(req_state)
            self.assertEqual(req_state["status"], "failed")
            self.assertIn("Ollama local connection refused", req_state["error_detail"])

            # Segundo intento con el mismo client_message_id -> devuelve el estado fallido deduplicado sin volver a llamar a Ollama
            second_res = await router.chat(
                user_message="Hola Yedid",
                conversation_id=cid,
                client_message_id="cmsg_fail_01",
            )
            self.assertTrue(second_res["deduplicated"])
            self.assertEqual(second_res["request_status"], "failed")
            self.assertEqual(second_res["reply"], first_res["reply"])
            self.assertEqual(ollama_attempts, 1)

    async def test_11_task_engine_cancel_vs_completion_race_condition(self) -> None:
        """
        Hallazgo #6: Verificar que:
        1) Si una tarea en cola (`queued`) es cancelada antes de que `_execute_task_pipeline` inicie,
           `_execute_task_pipeline` no la cambie a `running` ni la ejecute.
        2) Si una tarea en ejecución (`running`) es cancelada mientras el adaptador termina,
           el estado final en SQLite permanezca estrictamente en `'cancelled'`.
        """
        sandbox_dir = self.tmp_path / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)

        with patch("hub.task_engine.validate_project_path", return_value=(True, sandbox_dir, "OK")), \
             patch("hub.task_engine.is_sensitive_path", return_value=False), \
             patch("hub.task_engine.classify_task_risk", return_value="LOW"):

            t1 = self.engine.create_task(
                agent="safe_script",
                instruction="Tarea en cola a cancelar antes de correr",
                project_path=str(sandbox_dir),
            )
            tid1 = t1["id"]
            self.assertEqual(t1["status"], "queued")

            cancel_res1 = self.engine.cancel_task(tid1)
            self.assertTrue(cancel_res1)
            self.assertEqual(self.engine.get_task(tid1)["status"], "cancelled")

            await self.engine._execute_task_pipeline(t1)
            self.assertEqual(self.engine.get_task(tid1)["status"], "cancelled")

            t2 = self.engine.create_task(
                agent="safe_script",
                instruction="Tarea en ejecución cancelada durante el subproceso",
                project_path=str(sandbox_dir),
            )
            tid2 = t2["id"]

            async def simulated_adapter_with_concurrent_cancel(**kwargs):
                self.engine.cancel_task(tid2)
                return {
                    "success": True,
                    "returncode": 0,
                    "stdout": "Terminó justo después del cancel",
                    "stderr": "",
                    "deliverables": [],
                    "error": None,
                }

            with patch.object(self.engine._script_adapter, "execute_task", side_effect=simulated_adapter_with_concurrent_cancel):
                await self.engine._execute_task_pipeline(t2)

            final_t2 = self.engine.get_task(tid2)
            self.assertEqual(
                final_t2["status"],
                "cancelled",
                f"El estado 'cancelled' fue sobrescrito por '{final_t2['status']}'",
            )

    def test_12_github_sync_remote_deletion_protection(self) -> None:
        """
        Punto 4: Verificar que `scripts/github_sync.py` detecte archivos remotos que falten en la
        copia local (`missing_locally`), bloquee el push por defecto si no hay autorización explícita,
        permita preservarlos con `--preserve-remote` (`base_tree`), y solo envíe `"sha": None`
        cuando se autorice explícitamente con `--allow-delete`.
        """
        remote_paths = {"README.md", "hub/server.py", "docs/remote_only_notes.md"}
        local_paths = {"README.md", "hub/server.py", "tests/new_test.py"}

        diff = inspect_remote_tree_diff(remote_paths, local_paths)
        self.assertEqual(diff["added_locally"], ["tests/new_test.py"])
        self.assertEqual(diff["missing_locally"], ["docs/remote_only_notes.md"])

        local_entries = [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "sha_readme"},
            {"path": "hub/server.py", "mode": "100644", "type": "blob", "sha": "sha_server"},
            {"path": "tests/new_test.py", "mode": "100644", "type": "blob", "sha": "sha_test"},
        ]

        # 1. Sin autorización (--allow-delete=False, --preserve-remote=False) -> debe abortar con RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            build_tree_payload(
                local_entries=local_entries,
                base_tree_sha="base_tree_123",
                missing_locally=diff["missing_locally"],
                allow_delete=False,
                preserve_remote_missing=False,
            )
        self.assertIn("docs/remote_only_notes.md", str(ctx.exception))
        self.assertIn("--allow-delete", str(ctx.exception))

        # 2. Con --preserve-remote -> conserva archivos remotos usando base_tree sin entradas sha=None
        preserved_payload = build_tree_payload(
            local_entries=local_entries,
            base_tree_sha="base_tree_123",
            missing_locally=diff["missing_locally"],
            allow_delete=False,
            preserve_remote_missing=True,
        )
        self.assertEqual(preserved_payload["base_tree"], "base_tree_123")
        self.assertFalse(any(item.get("sha") is None for item in preserved_payload["tree"]))

        # 3. Con --allow-delete autorizado explícitamente -> incluye entrada sha=None para el archivo autorizado
        delete_payload = build_tree_payload(
            local_entries=local_entries,
            base_tree_sha="base_tree_123",
            missing_locally=diff["missing_locally"],
            allow_delete=True,
            preserve_remote_missing=False,
        )
        self.assertEqual(delete_payload["base_tree"], "base_tree_123")
        deleted_entries = [item for item in delete_payload["tree"] if item.get("sha") is None]
        self.assertEqual(len(deleted_entries), 1)
        self.assertEqual(deleted_entries[0]["path"], "docs/remote_only_notes.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
