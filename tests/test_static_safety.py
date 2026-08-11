import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StaticProcessSafetyTests(unittest.TestCase):
    def test_blocking_subprocess_helpers_have_timeout_and_never_use_shell(self):
        issues: list[str] = []
        for directory in (ROOT / "src", ROOT / "scripts"):
            for path in directory.rglob("*.py"):
                tree = ast.parse(
                    path.read_text(encoding="utf-8-sig"), filename=str(path)
                )
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = ""
                    if (
                        isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                    ):
                        name = f"{node.func.value.id}.{node.func.attr}"
                    keywords = {item.arg: item.value for item in node.keywords}
                    if name in {
                        "subprocess.run",
                        "subprocess.check_call",
                        "subprocess.check_output",
                    } and "timeout" not in keywords:
                        issues.append(f"{path.relative_to(ROOT)}:{node.lineno}: нет timeout")
                    shell = keywords.get("shell")
                    if shell is not None and not (
                        isinstance(shell, ast.Constant) and shell.value in {False, None}
                    ):
                        issues.append(f"{path.relative_to(ROOT)}:{node.lineno}: небезопасный shell")
                    if name == "os.system":
                        issues.append(f"{path.relative_to(ROOT)}:{node.lineno}: os.system")
        self.assertEqual(issues, [])

    def test_network_requests_have_explicit_timeouts(self):
        issues: list[str] = []
        for directory in (ROOT / "src", ROOT / "scripts"):
            for path in directory.rglob("*.py"):
                tree = ast.parse(
                    path.read_text(encoding="utf-8-sig"), filename=str(path)
                )
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    dotted = ""
                    if isinstance(node.func, ast.Attribute):
                        parts = [node.func.attr]
                        value = node.func.value
                        while isinstance(value, ast.Attribute):
                            parts.append(value.attr)
                            value = value.value
                        if isinstance(value, ast.Name):
                            parts.append(value.id)
                        dotted = ".".join(reversed(parts))
                    is_urlopen = dotted.endswith("urllib.request.urlopen")
                    is_search_opener = (
                        dotted.endswith(".open")
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "request"
                    )
                    if not (is_urlopen or is_search_opener):
                        continue
                    keywords = {item.arg for item in node.keywords}
                    if "timeout" not in keywords:
                        issues.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: сетевой вызов без timeout"
                        )
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
