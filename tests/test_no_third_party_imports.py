"""Shipped code must import only the standard library — candidates run this on
their own machines and a pip install is a support burden and a failure mode."""
import ast
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cadra-trace-tracker"
ALLOWED = set(sys.stdlib_module_names) | {"cadra"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_shipped_code_imports_stdlib_only():
    offenders: dict[str, set[str]] = {}
    for path in PLUGIN.rglob("*.py"):
        extra = _imported_roots(path) - ALLOWED
        if extra:
            offenders[str(path.relative_to(PLUGIN))] = extra
    assert offenders == {}, f"third-party imports in shipped code: {offenders}"
