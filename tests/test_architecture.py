"""Functional core, imperative shell: which libraries each layer may import.

`tach.toml` holds the edges between this package's layers. This holds the edges to everything
else: the domain is pure -- validation and containers, no I/O, no framework, no database -- and the
application adds nothing but the ports it declares. A database library in either would make
changing the database a change to the core, which is what the layering exists to prevent.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "aart_cli_telemetry_server"
DEPLOY = PACKAGE.parent / "deploy"

# mutmut rewrites the modules it mutates and adds its own imports to them; what is imported is a
# property of the source, which every ordinary run checks.
pytestmark = pytest.mark.skipif(
    "MUTANT_UNDER_TEST" in os.environ, reason="the modules are mutmut's rewrites"
)

_PURE_STDLIB = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "re",
    "typing",
}
ALLOWED = {
    "domain": _PURE_STDLIB | {"pydantic", "pydantic_core", "returns"},
    "application": _PURE_STDLIB | {"returns"},
}


def _raw_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _imports(path: Path) -> set[str]:
    return _raw_imports(path) - {"aart_cli_telemetry_server"}


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_a_core_layer_imports_only_what_it_may(layer: str) -> None:
    modules = sorted((PACKAGE / layer).rglob("*.py"))
    assert modules
    for module in modules:
        assert _imports(module) <= ALLOWED[layer], module.name


def test_the_check_sees_a_forbidden_import() -> None:
    assert "sqlalchemy" in _imports(PACKAGE / "adapters" / "sql.py")
    assert "fastapi" in _imports(PACKAGE / "web" / "app.py")


def test_deploying_imports_the_standard_library_alone_and_the_server_never_imports_it() -> None:
    modules = sorted(DEPLOY.rglob("*.py"))
    assert modules
    for module in modules:
        imported = _raw_imports(module) - {"deploy"}
        assert imported <= sys.stdlib_module_names, (module.name, imported)
    for module in sorted(PACKAGE.rglob("*.py")):
        assert "deploy" not in _imports(module), module.name
