"""The free tool never imports the gateway package, so it can be copied into its own repo as is."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "portward_free"


def test_no_module_imports_the_gateway_package() -> None:
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            offenders += [f"{path.name}: {n}" for n in names if n == "portward" or n.startswith("portward.")]
    assert offenders == []
