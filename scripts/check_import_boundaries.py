"""Check dependency-direction rules that keep execution code replaceable."""

from __future__ import annotations

import ast
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
        "packages",
        ("parsl",),
        top_level_only=True,
        production_only=True,
    ),
)


def _imports(
    tree: ast.Module, *, top_level_only: bool
) -> list[ast.Import | ast.ImportFrom]:
    nodes = tree.body if top_level_only else ast.walk(tree)
    return [node for node in nodes if isinstance(node, (ast.Import, ast.ImportFrom))]


def _imported_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module or ""]


def violations(root: Path = ROOT) -> list[str]:
    """Return forbidden imports with their source locations."""
    failures: set[str] = set()
    for rule in RULES:
        for path in sorted((root / rule.path).rglob("*.py")):
            if rule.production_only and "tests" in path.relative_to(root).parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in _imports(tree, top_level_only=rule.top_level_only):
                for imported_name in _imported_names(node):
                    if any(
                        imported_name == prefix
                        or imported_name.startswith(f"{prefix}.")
                        for prefix in rule.forbidden
                    ):
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
