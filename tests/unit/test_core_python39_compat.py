"""Regression tests for bioimageflow-core Python 3.9 compatibility."""

import ast
from pathlib import Path


CORE_PACKAGE = Path(__file__).parents[2] / "packages" / "bioimageflow-core" / "bioimageflow_core"


def _annotation_nodes(tree: ast.AST) -> list[ast.AST]:
    annotations: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
    return annotations


def test_core_annotations_do_not_use_pep604_union_syntax() -> None:
    """Python 3.9 evaluates annotations eagerly and cannot import ``type | None`` annotations."""
    offenders: list[str] = []

    for source_path in sorted(CORE_PACKAGE.glob("*.py")):
        source = source_path.read_text()
        tree = ast.parse(source, filename=str(source_path), feature_version=(3, 9))
        for annotation in _annotation_nodes(tree):
            if any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) for node in ast.walk(annotation)):
                offenders.append(f"{source_path.relative_to(CORE_PACKAGE)}:{annotation.lineno}")

    assert offenders == []
