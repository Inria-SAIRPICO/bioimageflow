"""Check dependency-direction rules that keep execution code replaceable."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from dataclasses import dataclass
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ImportRule:
    path: str
    forbidden: tuple[str, ...]
    top_level_only: bool = False
    production_only: bool = False


RULES = (
    ImportRule(
        "packages/bioimageflow/bioimageflow/storage",
        (
            "bioimageflow.backends",
            "bioimageflow.cache",
            "bioimageflow.engine",
            "bioimageflow.parsl",
            "bioimageflow.workflow",
            "parsl",
        ),
    ),
    ImportRule(
        "packages/bioimageflow/bioimageflow/cache",
        (
            "bioimageflow.backends",
            "bioimageflow.engine",
            "bioimageflow.parsl",
            "bioimageflow.workflow",
            "parsl",
        ),
    ),
    ImportRule(
        "packages/bioimageflow/bioimageflow/engine",
        ("bioimageflow.parsl", "bioimageflow.workflow", "parsl"),
    ),
    ImportRule(
        "packages/bioimageflow-core/bioimageflow_core",
        ("bioimageflow.parsl", "parsl"),
    ),
    ImportRule(
        "packages/bioimageflow-core/bioimageflow_core",
        ("bioimageflow", "pandas", "pydantic"),
        top_level_only=True,
    ),
    ImportRule(
        "packages/bioimageflow-core/bioimageflow_core/worker.py",
        ("bioimageflow", "pandas", "pydantic"),
    ),
    ImportRule(
        "packages",
        ("parsl",),
        top_level_only=True,
        production_only=True,
    ),
)


def _imports(
    tree: ast.Module, *, top_level_only: bool
) -> list[ast.Import | ast.ImportFrom]:
    if not top_level_only:
        return [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    result: list[ast.Import | ast.ImportFrom] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.append(node)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child)

    visit(tree)
    return result


def _package_name(path: Path, root: Path) -> str:
    # Workspace layouts place each import package below packages/<distribution>.
    parts = path.relative_to(root / "packages").parts[1:-1]
    return ".".join(parts)


def _imported_names(
    node: ast.Import | ast.ImportFrom, *, package: str
) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if node.level:
        try:
            module = resolve_name("." * node.level + module, package)
        except ImportError:
            return []
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def violations(root: Path = ROOT) -> list[str]:
    """Return forbidden imports with their source locations."""
    failures: set[str] = set()
    for rule in RULES:
        selected = root / rule.path
        paths = [selected] if selected.is_file() else sorted(selected.rglob("*.py"))
        for path in paths:
            if rule.production_only and "tests" in path.relative_to(root).parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in _imports(tree, top_level_only=rule.top_level_only):
                reported_edges: list[str] = []
                for imported_name in _imported_names(node, package=_package_name(path, root)):
                    if any(imported_name.startswith(f"{edge}.") for edge in reported_edges):
                        continue
                    if any(
                        imported_name == prefix
                        or imported_name.startswith(f"{prefix}.")
                        for prefix in rule.forbidden
                    ):
                        reported_edges.append(imported_name)
                        relative = path.relative_to(root).as_posix()
                        failures.add(
                            f"{relative}:{node.lineno}: forbidden import {imported_name}"
                        )
    return sorted(failures)


def main() -> int:
    failures = violations()
    if failures:
        print("Import-boundary guardrail failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Import-boundary guardrail passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
