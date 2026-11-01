"""Every module the command-line scripts import exists (memory_backfill imported a 'config' that didn't)."""
import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = sorted((ROOT / "cli").glob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_import_resolves(script, monkeypatch):
    # The scripts run with the repo root, cli/ and backend/ importable (they add these themselves).
    for folder in (ROOT, ROOT / "cli", ROOT / "backend"):
        monkeypatch.syspath_prepend(str(folder))
    missing = []
    for name in sorted(_imported_modules(script)):
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except ModuleNotFoundError:
            missing.append(name)
    assert missing == [], f"{script.name} imports modules that don't exist: {missing}"
