import tempfile
import unittest
import copy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from butler.config import load_settings
from butler.tasking import TaskCancelled
from butler.tools import ToolExecutor, tool_schemas
from butler.windows_bridge import WindowsBridgeError


class ToolExecutorTests(unittest.TestCase):
    def test_workspace_search_observes_cooperative_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = load_settings()
            raw = copy.deepcopy(original.raw)
            raw.setdefault("developer", {})["workspace_dir"] = "workspace"
            settings = replace(
                original,
                root=root,
                raw=raw,
                runtime_dir=root / "runtime",
            )
            executor = ToolExecutor(settings)
            for index in range(4):
                (executor.workspace_root / f"file-{index}.txt").write_text(
                    "needle",
                    encoding="utf-8",
                )
            checkpoints = 0

            def checkpoint() -> None:
                nonlocal checkpoints
                checkpoints += 1
                if checkpoints >= 3:
                    raise TaskCancelled("остановлено")

            with self.assertRaises(TaskCancelled):
                executor.execute(
                    "search_workspace",
                    {"path": ".", "query": "needle"},
                    confirmed=True,
                    checkpoint=checkpoint,
                )

        self.assertEqual(checkpoints, 3)

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

    def test_secret_directories_are_never_returned_or_searched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = load_settings()
            raw = copy.deepcopy(original.raw)
            raw.setdefault("developer", {})["workspace_dir"] = "workspace"
            settings = replace(
                original,
                root=root,
                raw=raw,
                runtime_dir=root / "runtime",
            )
            executor = ToolExecutor(settings)
            secret = executor.workspace_root / ".ssh" / "config"
            secret.parent.mkdir(parents=True)
            secret.write_text("IdentityFile do-not-expose", encoding="utf-8")

            read = executor.execute("read_workspace_file", {"path": ".ssh/config"})
            search = executor.execute(
                "search_workspace", {"path": ".", "query": "do-not-expose"}
            )

        self.assertFalse(read.ok)
        self.assertEqual(read.status, "sensitive_file")
        self.assertNotIn("do-not-expose", str(read.as_dict()))
        self.assertTrue(search.ok)
        self.assertEqual(search.data["matches"], [])

    def test_tool_arguments_are_validated_before_dispatch(self):
        executor = ToolExecutor(load_settings())

        cases = [
            ("read_workspace_file", {}, "missing required property 'path'"),
            ("list_procedures", {"unexpected": True}, "unexpected property 'unexpected'"),
            (
                "search_workspace",
                {"query": "needle", "case_sensitive": "false"},
                "property 'case_sensitive' must be boolean",
            ),
            ("forget_information", {"id": True}, "property 'id' must be integer"),
        ]
        for name, arguments, expected_message in cases:
            with self.subTest(tool=name, arguments=arguments):
                result = executor.execute(name, arguments, confirmed=True)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "invalid_arguments")
                self.assertIn(expected_message, result.message)

    def test_external_web_request_after_local_read_requires_confirmation(self):
        executor = ToolExecutor(load_settings())
        self.assertTrue(executor.execute("list_workspace").ok)

        guarded = executor.execute(
            "browser_search", {"query": "не отправлять без подтверждения"}
        )

        self.assertFalse(guarded.ok)
        self.assertEqual(guarded.status, "confirmation_required")
        remaining = executor._max_directory_list_calls - 1
        for _ in range(remaining):
            self.assertTrue(executor.execute("list_workspace").ok)
        walk_blocked = executor.execute("list_workspace")
        self.assertEqual(walk_blocked.status, "directory_walk_limit")
        executor.begin_task()
        self.assertFalse(executor._local_data_exposed)
        self.assertTrue(executor.execute("list_workspace").ok)

    @patch("butler.tools.active_window", return_value={"title": "Личный документ"})
    def test_external_web_request_after_active_window_read_requires_confirmation(
        self, _active_window
    ):
        executor = ToolExecutor(load_settings())
        self.assertTrue(executor.execute("windows_active_window").ok)

        guarded = executor.execute(
            "browser_search", {"query": "не отправлять заголовок окна"}
        )

        self.assertFalse(guarded.ok)
        self.assertEqual(guarded.status, "confirmation_required")

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

    def test_legacy_tool_log_never_records_argument_values(self):
        with tempfile.TemporaryDirectory() as directory:
            original = load_settings()
            raw = copy.deepcopy(original.raw)
            raw.setdefault("diagnostics", {})["enabled"] = True
            raw["diagnostics"]["allow_during_tests"] = True
            settings = replace(
                original,
                raw=raw,
                runtime_dir=Path(directory) / "runtime",
            )
            executor = ToolExecutor(settings)
            secret = "unknown-field-must-never-reach-log"

            executor.execute("unknown-test-tool", {"unrecognized_payload": secret})
            log_text = executor.log_path.read_text(encoding="utf-8")

        self.assertNotIn(secret, log_text)
        self.assertNotIn('"args"', log_text)
        self.assertIn('"argument_names"', log_text)

    def test_nested_browser_actions_and_windows_selectors_are_strict(self):
        original = load_settings()
        raw = copy.deepcopy(original.raw)
        raw["browser"]["active_control_enabled"] = True
        raw["windows"]["active_control_enabled"] = True
        executor = ToolExecutor(replace(original, raw=raw))
        executor.browser.interact = MagicMock()
        executor.windows.execute = MagicMock()

        browser = executor.execute(
            "browser_interact",
            {
                "url": "https://example.com",
                "actions": [
                    {
                        "type": "fill",
                        "selector": "#message",
                        "text": "safe",
                        "unrecognized_payload": "secret",
                    }
                ],
            },
            confirmed=True,
        )
        windows = executor.execute(
            "windows_invoke_control",
            {
                "selector": {
                    "name": "OK",
                    "unrecognized_payload": "secret",
                }
            },
            confirmed=True,
        )

        self.assertEqual(browser.status, "invalid_arguments")
        self.assertEqual(windows.status, "invalid_arguments")
        executor.browser.interact.assert_not_called()
        executor.windows.execute.assert_not_called()

    def test_read_and_list_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "default.json").write_text(
                '{"paths":{"llama_server":"server.exe","models_dir":"models","runtime_dir":"runtime"},'
                '"assistant":{"name":"Тест","default_role":"dev"},"server":{"port":1},'
                '"models":{"dev":{"label":"Dev","filename":"dev.gguf","context_size":512,"gpu_layers":1}},'
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

    def test_write_and_project_tests_require_confirmation(self):
        settings = load_settings()
        tools = ToolExecutor(settings)
        with tempfile.TemporaryDirectory(dir=tools.workspace_root) as directory:
            target = Path(directory) / "created.txt"
            relative = str(target.relative_to(tools.workspace_root))
            pending = tools.execute(
                "write_workspace_file", {"path": relative, "content": "исходный текст"}
            )
            self.assertEqual(pending.status, "confirmation_required")
            created = tools.execute(
                "write_workspace_file",
                {"path": relative, "content": "исходный текст"},
                confirmed=True,
            )
            self.assertTrue(created.ok)
            overwrite = tools.execute(
                "write_workspace_file",
                {"path": relative, "content": "ПРОЧИТАТЬ"},
                confirmed=True,
            )
            self.assertFalse(overwrite.ok)
            self.assertEqual(overwrite.status, "existing_file_requires_replace")
            self.assertEqual(target.read_text(encoding="utf-8"), "исходный текст")
        tests_pending = tools.execute("run_project_tests", {"cwd": "."})
        self.assertEqual(tests_pending.status, "confirmation_required")

    def test_outside_file_is_denied(self):
        settings = load_settings()
        tools = ToolExecutor(settings)
        result = tools.execute("read_workspace_file", {"path": str(Path.home() / "outside.txt")})
        self.assertEqual(result.status, "denied")

    def test_delete_rejects_workspace_symbolic_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = load_settings()
            raw = copy.deepcopy(original.raw)
            raw.setdefault("developer", {})["workspace_dir"] = "workspace"
            settings = replace(
                original,
                root=root,
                raw=raw,
                runtime_dir=root / "runtime",
            )
            executor = ToolExecutor(settings)
            target = executor.workspace_root / "real.txt"
            target.write_text("keep", encoding="utf-8")
            link = executor.workspace_root / "link.txt"
            link.write_text("link placeholder", encoding="utf-8")

            with patch.object(Path, "is_symlink", return_value=True):
                result = executor.execute(
                    "delete_workspace_file", {"path": "link.txt"}, confirmed=True
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.status, "symlink_rejected")
            self.assertTrue(link.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_active_windows_control_is_hidden_until_explicitly_enabled(self):
        original = load_settings()
        names = {item["function"]["name"] for item in tool_schemas(original)}
        self.assertIn("windows_inspect_controls", names)
        self.assertNotIn("windows_invoke_control", names)
        self.assertNotIn("windows_move_pointer", names)
        self.assertNotIn("windows_click_pointer", names)

        with patch("butler.tools.move_pointer") as move:
            blocked = ToolExecutor(original).execute(
                "windows_move_pointer", {"x": 10, "y": 10}, confirmed=True
            )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "disabled")
        move.assert_not_called()

        raw = copy.deepcopy(original.raw)
        raw["windows"]["active_control_enabled"] = True
        enabled = replace(original, raw=raw)
        enabled_names = {
            item["function"]["name"] for item in tool_schemas(enabled)
        }
        self.assertIn("windows_invoke_control", enabled_names)
        self.assertIn("windows_move_pointer", enabled_names)
        self.assertIn("windows_click_pointer", enabled_names)

    def test_local_knowledge_tools_are_exposed_and_writes_confirmed(self):
        names = {item["function"]["name"] for item in tool_schemas()}
        self.assertIn("recall_information", names)
        self.assertIn("remember_information", names)
        self.assertIn("forget_information", names)
        self.assertNotIn("browser_send_message", names)
        self.assertIn("list_procedures", names)
        self.assertIn("read_procedure", names)
        configured_schemas = tool_schemas(load_settings())
        read_procedure = next(
            item for item in configured_schemas
            if item["function"]["name"] == "read_procedure"
        )
        procedure_names = read_procedure["function"]["parameters"]["properties"]["name"]["enum"]
        self.assertIn("development", procedure_names)
        pending = ToolExecutor(load_settings()).execute(
            "remember_information", {"text": "локальный тест"}
        )
        self.assertEqual(pending.status, "confirmation_required")

    def test_active_browser_control_is_hidden_and_blocked_until_safe_adapter_enabled(self):
        original = load_settings()
        default_names = {item["function"]["name"] for item in tool_schemas(original)}
        self.assertNotIn("browser_interact", default_names)
        self.assertNotIn("browser_send_message", default_names)

        blocked = ToolExecutor(original).execute(
            "browser_interact",
            {"url": "https://example.com", "actions": [{"type": "wait"}]},
            confirmed=True,
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "disabled")

        raw = copy.deepcopy(original.raw)
        raw["browser"]["active_control_enabled"] = True
        enabled = replace(original, raw=raw)
        enabled_names = {item["function"]["name"] for item in tool_schemas(enabled)}
        self.assertIn("browser_interact", enabled_names)
        self.assertIn("browser_send_message", enabled_names)

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

    def test_rag_no_match_is_reported_without_random_fragments(self):
        original = load_settings()
        raw = copy.deepcopy(original.raw)
        raw["rag"]["enabled"] = True
        settings = replace(original, raw=raw)
        executor = ToolExecutor(settings)
        executor.embedder = MagicMock()
        executor.rag.index_workspace = MagicMock(return_value=None)
        executor.rag.search = MagicMock(return_value=[])

        result = executor.execute(
            "search_project_knowledge", {"query": "несвязанный вопрос"}
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "no_match")
        self.assertIn("надёжного совпадения", result.message.casefold())
        self.assertEqual(result.data["items"], [])

    def test_windows_financial_context_cannot_use_generic_keyboard_tool(self):
        original = load_settings()
        raw = copy.deepcopy(original.raw)
        raw["windows"]["active_control_enabled"] = True
        executor = ToolExecutor(replace(original, raw=raw))
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

    def test_windows_write_fails_closed_when_window_context_is_unavailable(self):
        original = load_settings()
        raw = copy.deepcopy(original.raw)
        raw["windows"]["active_control_enabled"] = True
        executor = ToolExecutor(replace(original, raw=raw))
        with (
            patch(
                "butler.tools.active_window",
                side_effect=WindowsBridgeError("UI недоступен"),
            ),
            patch("butler.tools.press_keys") as press,
        ):
            result = executor.execute(
                "windows_press_keys", {"keys": "ENTER"}, confirmed=True
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "window_context_unavailable")
        press.assert_not_called()

    def test_uia_write_requires_exact_target_window(self):
        original = load_settings()
        raw = copy.deepcopy(original.raw)
        raw["windows"]["active_control_enabled"] = True
        executor = ToolExecutor(replace(original, raw=raw))
        with (
            patch("butler.tools.list_windows", return_value=[]),
            patch.object(executor.windows, "execute") as execute,
        ):
            result = executor.execute(
                "windows_invoke_control",
                {"handle": 999, "selector": {"automation_id": "confirm"}},
                confirmed=True,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "window_context_unavailable")
        execute.assert_not_called()

    def test_pointer_movement_requires_confirmation_before_side_effect(self):
        original = load_settings()
        raw = copy.deepcopy(original.raw)
        raw["windows"]["active_control_enabled"] = True
        tools = ToolExecutor(replace(original, raw=raw))
        result = tools.execute("windows_move_pointer", {"x": 10, "y": 10})
        self.assertEqual(result.status, "confirmation_required")


if __name__ == "__main__":
    unittest.main()
