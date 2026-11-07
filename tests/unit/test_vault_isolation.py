"""``joyread.core.vault`` imports nothing from Qt or the rest of JoyRead.

The format has to stay testable, specifiable and reusable without the app.
An import of UI or service code would also let format behavior depend on
application state, which is exactly what a sealed file must never do.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys


VAULT_ROOT = Path(__file__).resolve().parents[2] / "src" / "joyread" / "core" / "vault"
ALLOWED_THIRD_PARTY = {"Cryptodome"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_the_vault_package_depends_only_on_the_stdlib_and_cryptodome() -> None:
    sources = sorted(VAULT_ROOT.glob("*.py"))
    assert sources, "the vault package moved; update this guard"

    offenders = {}
    for path in sources:
        for module in _imported_modules(path):
            top = module.split(".", 1)[0]
            if module == "__future__" or top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY:
                continue
            if module == "joyread.core.vault" or module.startswith("joyread.core.vault."):
                continue
            offenders.setdefault(path.name, []).append(module)

    assert offenders == {}
