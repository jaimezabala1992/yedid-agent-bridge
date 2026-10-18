"""
tests/test_chatgpt_audit_fixes.py

Suite automatizada de verificación en entorno aislado para los 6 hallazgos
identificados en la revisión de arquitectura de ChatGPT (Yedid):

1. Consultas SQL parametrizadas sin llamadas a `os.getenv()` dentro de SQL en `hub/chat_history_db.py`.
2. Neutralización de inyección HTML/JS (XSS) en `hub/static/index.html` (`formatChatTextHtml`, `renderTaskCardHtml`, `renderApprovalsModal`, `appendMessage`).
3. Prevención de inyección de código en `SafeScriptAdapter.execute_task` (`hub/agent_adapters.py`).
4. Recuperación de los mensajes más recientes (y no los más antiguos) en `get_recent_context_for_llm()`.
5. Deduplicación determinista de respuestas y tareas asociadas por `client_message_id` en `hub/llm_router.py`.
6. Ausencia de condiciones de carrera entre `cancel_task()` y `_execute_task_pipeline()` en `hub/task_engine.py`.
"""

import asyncio
import inspect
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Asegurar que el workspace aislado esté al inicio de sys.path y no escriba tokens ni logs en data/
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("YEDID_TOKEN", "test_isolated_token_ci_only")

import hub.audit as _hub_audit
_hub_audit.AUDIT_LOG_FILE = Path(tempfile.gettempdir()) / "yedid_audit_test_isolated.jsonl"

from hub.chat_history_db import ChatHistoryDB
from hub.agent_adapters import SafeScriptAdapter
from hub.task_engine import TaskEngine


class TestChatGPTAuditFixes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="yedid_audit_test_")
        self.tmp_path = Path(self.tmp_dir.name)
        self.chat_db_path = self.tmp_path / "test_chat_history.db"
        self.tasks_db_path = self.tmp_path / "test_tasks.db"
        self.chat_db = ChatHistoryDB(db_path=self.chat_db_path)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_1_parameterized_sql_queries_in_chat_history_db(self):
        """
        Hallazgo #1: Verificar que ninguna sentencia SQL contenga `os.getenv` literal
        y que `list_conversations`, `get_conversation` y `delete_conversation` funcionen
        con parámetros vinculados en SQLite.
        """
        src = inspect.getsource(ChatHistoryDB)
        # Asegurar que no exista `os.getenv` dentro de una sentencia SQL IN (...)
        self.assertNotIn("IN ('jaime', os.getenv", src)

        os.environ["YEDID_OWNER_EMAIL"] = "custom_owner@example.org"
        conv = self.chat_db.create_conversation(user_id="custom_owner@example.org", title="Test SQL Param")
        cid = conv["id"]
        self.chat_db.add_message(cid, "user", "Hola mundo", client_message_id="c_sql_1")
        self.chat_db.add_message(cid, "assistant", "Respuesta OK")

        # Consultar filtrando por usuario 'jaime' (debe incluir owner_email parametrizado)
        listed = self.chat_db.list_conversations(user="jaime", limit=10)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], cid)
        self.assertEqual(listed[0]["message_count"], 2)

        fetched = self.chat_db.get_conversation(cid, user="jaime")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["message_count"], 2)

        deleted = self.chat_db.delete_conversation(cid, user="jaime")
        self.assertTrue(deleted)
        self.assertIsNone(self.chat_db.get_conversation(cid))

    def test_2_xss_prevention_in_index_html(self):
        """
        Hallazgo #2: Verificar que `hub/static/index.html` defina `escapeHtml()`,
        elimine el bypass condicional `!safeText.includes('<div') && !safeText.includes('<strong')`
        en `formatChatTextHtml()`, y escape los campos dinámicos en tarjetas y modales.
        """
        index_path = REPO_ROOT / "hub" / "static" / "index.html"
        html_src = index_path.read_text(encoding="utf-8")

        # 1. Debe existir la función escapeHtml
        self.assertIn("function escapeHtml(val)", html_src)

        # 2. No debe existir el bypass inseguro de <div o <strong en formatChatTextHtml
        self.assertNotIn("safeText.includes('<div')", html_src)
        self.assertNotIn("safeText.includes('<strong')", html_src)

        # 3. formatChatTextHtml debe invocar escapeHtml(safeText) incondicionalmente antes del reemplazo Markdown
        fmt_match = re.search(
            r"function formatChatTextHtml\(rawText,\s*tools\s*=\s*\[\]\)\s*\{(.*?)return safeText;\s*\}",
            html_src,
            re.DOTALL,
        )
        self.assertIsNotNone(fmt_match, "No se encontró formatChatTextHtml en index.html")
        fmt_body = fmt_match.group(1)
        self.assertIn("safeText = escapeHtml(safeText)", fmt_body)

        # Simular la lógica exacta de escapeHtml + formatChatTextHtml en Python para validar payloads XSS
        def py_escape_html(val: str) -> str:
            return (
                str(val)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;")
            )

        def py_format_chat_text_html(raw_text: str) -> str:
            safe_text = py_escape_html(raw_text.strip())
            safe_text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe_text)
            safe_text = re.sub(
                r"`([^`]+)`",
                r'<code style="background: rgba(255,255,255,0.1); padding: 1px 4px; border-radius: 3px; font-family: monospace; font-size: 0.9em;">\1</code>',
                safe_text,
            )
            safe_text = safe_text.replace("\n", "<br>")
            return safe_text

        xss_payloads = [
            "<div onmouseover=\"alert('xss')\"> Ataque </div>",
            "<strong onclick=alert(1)>Texto</strong><script>evil()</script>",
            "<img src=x onerror=alert(document.cookie)> **Negrita Segura** `codigo`",
        ]
        for payload in xss_payloads:
            rendered = py_format_chat_text_html(payload)
            self.assertNotIn("<script", rendered)
            self.assertNotIn("<img", rendered)
            self.assertNotIn("<div", rendered)
            self.assertNotIn("<strong onclick", rendered)
            self.assertIn("&lt;", rendered)

        self.assertIn("<strong>Negrita Segura</strong>", py_format_chat_text_html(xss_payloads[2]))

        # 4. Verificar que renderTaskCardHtml y renderApprovalsModal usen escapeHtml
        self.assertIn("const safeInstruction = escapeHtml(task.instruction", html_src)
        self.assertIn("const instructionText = escapeHtml(item.instruction", html_src)

    async def test_3_safe_script_adapter_prevents_code_injection(self):
        """
        Hallazgo #3: Verificar que `SafeScriptAdapter.execute_task` no interpole instrucciones
        dentro del código fuente Python generado y trate intentos de ruptura de comillas triples
        o f-strings como texto inerte.
        """
        sandbox_test_dir = self.tmp_path / "sandbox_test"
        sandbox_test_dir.mkdir(parents=True, exist_ok=True)
        marker_file = self.tmp_path / "pwned_marker.txt"

        malicious_instruction = (
            "Prueba ''' + str(open(r'"
            + str(marker_file)
            + "', 'w').write('PWNED')) + ''' y {__import__('os').system('echo PWNED')} \"\"\""
        )

        adapter = SafeScriptAdapter(python_path=Path(sys.executable))
        with patch("hub.security_policy.ALLOWED_PROJECT_ROOTS", [self.tmp_path.resolve()]):
            res = await adapter.execute_task(
                instruction=malicious_instruction,
                project_path=sandbox_test_dir,
                timeout_seconds=15.0,
            )

        self.assertTrue(res["success"], f"SafeScriptAdapter falló: {res}")
        self.assertFalse(marker_file.exists(), "¡Vulnerabilidad de inyección de código detectada!")

        # Verificar que el script generado no contenga la instrucción incrustada en el código fuente
        generated_scripts = list(sandbox_test_dir.glob("task_exec_*.py"))
        self.assertEqual(len(generated_scripts), 1)
        script_source = generated_scripts[0].read_text(encoding="utf-8")
        self.assertNotIn("PWNED", script_source)
        self.assertIn('os.environ.get("YEDID_TASK_INSTRUCTION"', script_source)

        # Verificar que el archivo de resultado contenga el texto como dato inerte
        out_files = list(sandbox_test_dir.glob("resultado_tarea_*.txt"))
        self.assertEqual(len(out_files), 1)
        out_content = out_files[0].read_text(encoding="utf-8")
        self.assertIn("ESTADO: COMPLETADO CON ÉXITO".lower(), out_content.lower())

    def test_4_get_recent_context_for_llm_returns_newest_messages(self):
        """
        Hallazgo #4: Cuando una conversación supera `limit * 2` mensajes,
        `get_recent_context_for_llm(limit=4)` debe devolver los últimos 8 mensajes
        más recientes en orden cronológico ascendente, nunca los primeros 8 más antiguos.
        """
        conv = self.chat_db.create_conversation(user_id="jaime", title="Conversación Larga")
        cid = conv["id"]

        # Insertar 15 turnos (30 mensajes: Turno 1 a Turno 15)
        for i in range(1, 16):
            self.chat_db.add_message(cid, "user", f"Pregunta usuario {i}")
            self.chat_db.add_message(cid, "assistant", f"Respuesta asistente {i}")

        recent = self.chat_db.get_recent_context_for_llm(cid, limit=4)
        self.assertEqual(len(recent), 8)

        # Deben corresponder exactamente a los turnos 12, 13, 14 y 15 en orden cronológico
        expected = [
            {"role": "user", "content": "Pregunta usuario 12"},
            {"role": "assistant", "content": "Respuesta asistente 12"},
            {"role": "user", "content": "Pregunta usuario 13"},
            {"role": "assistant", "content": "Respuesta asistente 13"},
            {"role": "user", "content": "Pregunta usuario 14"},
            {"role": "assistant", "content": "Respuesta asistente 14"},
            {"role": "user", "content": "Pregunta usuario 15"},
            {"role": "assistant", "content": "Respuesta asistente 15"},
        ]
        self.assertEqual(recent, expected)

    async def test_5_deterministic_deduplication_by_client_message_id(self):
        """
        Hallazgo #5: Al reintentar un mensaje con `client_message_id` ya procesado,
        `LLMRouter.chat` debe devolver la respuesta del asistente y la tarea vinculada
        exactas de ese turno, incluso si hubo muchos mensajes posteriores en la conversación.
        """
        from hub.llm_router import LLMRouter

        conv = self.chat_db.create_conversation(user_id="jaime", title="Test Deduplicación")
        cid = conv["id"]

        # Turno 1
        self.chat_db.add_message(cid, "user", "Mensaje inicial 1", client_message_id="cmid_1")
        self.chat_db.add_message(cid, "assistant", "Respuesta inicial 1")

        # Turno 2 (con herramientas y tarea asociada)
        self.chat_db.add_message(cid, "user", "Ejecuta tarea X", client_message_id="cmid_target")
        self.chat_db.add_message(
            cid,
            "assistant",
            "Respuesta exacta del Turno 2 para cmid_target",
            tool_calls=[{"tool": "execute_agent_task", "result": {"id": "task_xyz123"}}],
            task_id="task_xyz123",
        )

        # Turnos 3, 4 y 5 posteriores
        for idx in range(3, 6):
            self.chat_db.add_message(cid, "user", f"Mensaje posterior {idx}", client_message_id=f"cmid_{idx}")
            self.chat_db.add_message(cid, "assistant", f"Respuesta posterior {idx}")

        fake_task = {"id": "task_xyz123", "status": "completed", "agent": "safe_script"}
        router_instance = LLMRouter()

        with patch("hub.llm_router.chat_history_db", self.chat_db), patch(
            "hub.llm_router.task_engine.get_task", return_value=fake_task
        ):
            dup_res = await router_instance.chat(
                user_message="Ejecuta tarea X",
                conversation_id=cid,
                client_message_id="cmid_target",
            )

        self.assertTrue(dup_res.get("deduplicated"))
        self.assertEqual(dup_res["reply"], "Respuesta exacta del Turno 2 para cmid_target")
        self.assertEqual(dup_res["task"], fake_task)
        self.assertEqual(len(dup_res["tools_executed"]), 1)
        self.assertEqual(dup_res["tools_executed"][0]["tool"], "execute_agent_task")

    async def test_6_task_engine_cancel_vs_completion_race_condition(self):
        """
        Hallazgo #6: Verificar que:
        a) Si una tarea en ejecución (`running`) se cancela mediante `cancel_task()`,
           cuando el adaptador termina NO sobrescribe el estado `'cancelled'` con `'failed'` ni `'completed'`.
        b) Si una tarea en cola (`queued`) se cancela justo antes de entrar a `_execute_task_pipeline()`,
           la transición Compare-And-Set aborta el inicio y conserva `'cancelled'`.
        """
        sandbox_dir = self.tmp_path / "race_sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        from hub.approvals import ApprovalManager
        isolated_approvals = ApprovalManager(storage_path=self.tmp_path / "test_approvals.json")

        with patch("hub.security_policy.ALLOWED_PROJECT_ROOTS", [self.tmp_path.resolve()]), patch(
            "hub.task_engine.classify_task_risk", return_value="LOW"
        ), patch("hub.task_engine.approval_manager", isolated_approvals), patch(
            "hub.task_engine.chat_history_db", self.chat_db
        ):
            engine = TaskEngine(db_path=self.tasks_db_path)

            # Caso A: Tarea cancelada mientras está en ejecución ('running')
            t1 = engine.create_task(
                agent="safe_script",
                instruction="Tarea en curso que será cancelada",
                project_path=str(sandbox_dir),
                risk_level="LOW",
            )
            t1_id = t1["id"]

            async def slow_adapter_execution(**kwargs):
                on_started = kwargs.get("on_process_started")
                if on_started:
                    on_started(999999)
                await asyncio.sleep(0.15)
                # Simular que el usuario cancela la tarea mientras el subproceso está corriendo
                cancelled_ok = engine.cancel_task(t1_id)
                self.assertTrue(cancelled_ok)
                await asyncio.sleep(0.05)
                # El proceso muere con returncode != 0 (que antes causaba status='failed')
                return {
                    "success": False,
                    "returncode": -9,
                    "stdout": "Parcial",
                    "stderr": "Terminated by signal",
                    "deliverables": [],
                }

            with patch.object(engine._script_adapter, "execute_task", side_effect=slow_adapter_execution):
                await engine._execute_task_pipeline(engine.get_task(t1_id))

            final_t1 = engine.get_task(t1_id)
            self.assertEqual(
                final_t1["status"],
                "cancelled",
                f"El estado 'cancelled' fue sobrescrito por '{final_t1['status']}'!",
            )
            self.assertEqual(final_t1["error"], "Cancelada por el usuario.")

            # Caso B: Tarea cancelada justo después de ser leída como 'queued', antes del UPDATE a 'running'
            t2 = engine.create_task(
                agent="safe_script",
                instruction="Tarea cancelada antes de arrancar",
                project_path=str(sandbox_dir),
                risk_level="LOW",
            )
            t2_id = t2["id"]
            stale_queued_snapshot = engine.get_task(t2_id)
            # Cancelar antes de que _execute_task_pipeline haga el Compare-And-Set
            self.assertTrue(engine.cancel_task(t2_id))

            adapter_called = False

            async def should_not_run(**kwargs):
                nonlocal adapter_called
                adapter_called = True
                return {"success": True, "returncode": 0, "stdout": "", "stderr": "", "deliverables": []}

            with patch.object(engine._script_adapter, "execute_task", side_effect=should_not_run):
                await engine._execute_task_pipeline(stale_queued_snapshot)

            self.assertFalse(adapter_called, "El adaptador no debió ejecutarse para una tarea ya cancelada")
            final_t2 = engine.get_task(t2_id)
            self.assertEqual(final_t2["status"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
