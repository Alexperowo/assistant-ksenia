import tempfile
import unittest
import copy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from butler.config import load_settings
from butler.tools import ToolExecutor, tool_schemas


class ToolExecutorTests(unittest.TestCase):
    def test_secret_file_contents_are_never_returned_or_searched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = load_settings()
            raw = copy.deepcopy(original.raw)
            raw.setdefault("developer", {})["workspace_dir"] = "workspace"
            raw.setdefault("permissions", {})["allowed_roots"] = ["projects"]
            settings = replace(
                original,
                root=root,
                raw=raw,
                runtime_dir=root / "runtime",
            )
            executor = ToolExecutor(settings)
            secret = executor.workspace_root / ".env"
            secret.write_text("API_TOKEN=do-not-expose", encoding="utf-8")
            read = executor.execute("read_workspace_file", {"path": ".env"})
            search = executor.execute(
                "search_workspace", {"path": ".", "query": "do-not-expose"}
            )

        self.assertFalse(read.ok)
        self.assertEqual(read.status, "sensitive_file")
        self.assertNotIn("do-not-expose", str(read.as_dict()))
        self.assertTrue(search.ok)
        self.assertEqual(search.data["matches"], [])

    def test_external_web_request_after_local_read_requires_confirmation(self):
        executor = ToolExecutor(load_settings())
        self.assertTrue(executor.execute("list_workspace").ok)

        guarded = executor.execute(
            "browser_search", {"query": "не отправлять без подтверждения"}
        )

        self.assertFalse(guarded.ok)
        self.assertEqual(guarded.status, "confirmation_required")
        executor.begin_task()
        self.assertFalse(executor._local_data_exposed)

    def test_log_failure_does_not_change_tool_result(self):
        with tempfile.TemporaryDirectory() as directory:
            original = load_settings()
            raw = copy.deepcopy(original.raw)
            raw.setdefault("diagnostics", {})["allow_during_tests"] = True
            settings = replace(
                original,
                raw=raw,
                runtime_dir=Path(directory) / "runtime",
            )
            executor = ToolExecutor(settings)
            blocked_log_path = Path(directory) / "not-a-file"
            blocked_log_path.mkdir()
            executor.log_path = blocked_log_path
            result = executor.execute("unknown-test-tool")
            self.assertEqual(result.status, "unknown_tool")

    def test_resolved_paths_outside_workspace_are_rejected_by_search_guard(self):
        tools = ToolExecutor(load_settings())
        self.assertTrue(tools._inside_workspace(tools.workspace_root / "project.txt"))
        self.assertFalse(tools._inside_workspace(Path.home() / "private.txt"))

    def test_log_sanitizer_hides_command_arguments_urls_and_typed_text(self):
        executor = ToolExecutor(load_settings())
        safe = executor._safe_log_value(
            {
                "command": ["python.exe", "--token", "very-secret"],
                "url": "https://example.test/login?token=very-secret#private",
                "query": "личный поисковый запрос",
                "actions": [{"type": "fill", "text": "password"}],
            }
        )
        serialized = str(safe)
        self.assertIn("python.exe", serialized)
        self.assertIn("https://example.test/login", serialized)
        self.assertNotIn("very-secret", serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("личный поисковый запрос", serialized)

    def test_read_and_list_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "default.json").write_text(
                '{"paths":{"llama_server":"server.exe","models_dir":"models","runtime_dir":"runtime"},'
                '"assistant":{"name":"Тест","default_role":"dev"},"server":{"port":1},'
                '"models":{"dev":{"label":"Dev","filename":"dev.gguf","context_size":1,"gpu_layers":1}},'
                '"permissions":{"allowed_roots":["workspace"],"actions":{"list_directory":"allow","read_file":"allow","write_file":"confirm"}}}',
                encoding="utf-8",
            )
            settings = load_settings(root)
            (root / "note.txt").write_text("Привет", encoding="utf-8")
            tools = ToolExecutor(settings)
            self.assertTrue(tools.execute("list_workspace").ok)
            result = tools.execute("read_workspace_file", {"path": "note.txt"})
            self.assertEqual(result.data["content"], "Привет")

    def test_large_text_file_is_read_in_line_pages(self):
        settings = load_settings()
        tools = ToolExecutor(settings)
        target = tools.workspace_root / "pagination-test.txt"
        target.write_text("\n".join(f"line {number}" for number in range(1, 451)), encoding="utf-8")
        try:
            result = tools.execute(
                "read_workspace_file",
                {"path": target.name, "start_line": 201, "max_lines": 100},
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["start_line"], 201)
            self.assertEqual(result.data["end_line"], 300)
            self.assertEqual(result.data["total_lines"], 450)
            self.assertTrue(result.data["has_more"])
            self.assertIn("line 201", result.data["content"])
            self.assertNotIn("line 200\n", result.data["content"])
        finally:
            target.unlink(missing_ok=True)

    def test_write_requires_confirmation(self):
        settings = load_settings()
        tools = ToolExecutor(settings)
        pending = tools.execute("write_workspace_file", {"path": "runtime/test-tool.txt", "content": "x"})
        self.assertEqual(pending.status, "confirmation_required")

    def test_outside_file_is_denied(self):
        settings = load_settings()
        tools = ToolExecutor(settings)
        result = tools.execute("read_workspace_file", {"path": str(Path.home() / "outside.txt")})
        self.assertEqual(result.status, "denied")

    def test_windows_uia_and_pointer_tools_are_exposed(self):
        names = {item["function"]["name"] for item in tool_schemas()}
        self.assertIn("windows_inspect_controls", names)
        self.assertIn("windows_invoke_control", names)
        self.assertIn("windows_move_pointer", names)
        self.assertIn("windows_click_pointer", names)

    def test_local_knowledge_tools_are_exposed_and_writes_confirmed(self):
        names = {item["function"]["name"] for item in tool_schemas()}
        self.assertIn("recall_information", names)
        self.assertIn("remember_information", names)
        self.assertIn("forget_information", names)
        self.assertIn("browser_send_message", names)
        self.assertIn("list_procedures", names)
        self.assertIn("read_procedure", names)
        pending = ToolExecutor(load_settings()).execute(
            "remember_information", {"text": "локальный тест"}
        )
        self.assertEqual(pending.status, "confirmation_required")

    def test_rag_tool_is_only_exposed_when_enabled(self):
        original = load_settings()
        disabled_raw = copy.deepcopy(original.raw)
        disabled_raw["rag"]["enabled"] = False
        enabled_raw = copy.deepcopy(original.raw)
        enabled_raw["rag"]["enabled"] = True
        disabled_settings = replace(original, raw=disabled_raw)
        enabled_settings = replace(original, raw=enabled_raw)
        disabled = {
            item["function"]["name"] for item in tool_schemas(disabled_settings)
        }
        enabled = {
            item["function"]["name"] for item in tool_schemas(enabled_settings)
        }

        self.assertNotIn("search_project_knowledge", disabled)
        self.assertIn("search_project_knowledge", enabled)

    def test_windows_financial_context_cannot_use_generic_keyboard_tool(self):
        executor = ToolExecutor(load_settings())
        with (
            patch(
                "butler.tools.active_window",
                return_value={"handle": 10, "title": "Checkout — оплата заказа"},
            ),
            patch("butler.tools.press_keys") as press,
        ):
            result = executor.execute(
                "windows_press_keys", {"keys": "ENTER"}, confirmed=True
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "denied")
        press.assert_not_called()

    def test_pointer_movement_requires_confirmation_before_side_effect(self):
        tools = ToolExecutor(load_settings())
        result = tools.execute("windows_move_pointer", {"x": 10, "y": 10})
        self.assertEqual(result.status, "confirmation_required")


if __name__ == "__main__":
    unittest.main()
