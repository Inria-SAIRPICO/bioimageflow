"""Enforce maintainable source and test module size ceilings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SizeRule:
    pattern: str
    maximum: int
    label: str


RULES = (
    SizeRule("packages/bioimageflow/bioimageflow/**/*.py", 800, "orchestrator module"),
    SizeRule(
        "packages/bioimageflow-core/bioimageflow_core/**/*.py",
        800,
        "core production module",
    ),
    SizeRule("tests/**/test_*.py", 500, "test module"),
    SizeRule("tests/testkit/*.py", 700, "shared test helper"),
    SizeRule("tests/**/conftest.py", 700, "pytest configuration"),
)


def violations(root: Path = ROOT) -> list[str]:
    """Return human-readable size violations under *root*."""
    failures: list[str] = []
    checked: set[Path] = set()
    for rule in RULES:
        for path in sorted(root.glob(rule.pattern)):
            if path in checked or "__pycache__" in path.parts:
                continue
            checked.add(path)
            line_count = len(path.read_text().splitlines())
            if line_count > rule.maximum:
                relative = path.relative_to(root).as_posix()
                failures.append(
                    f"{relative}: {line_count} lines exceeds the {rule.maximum}-line {rule.label} limit"
                )
    return failures


def main() -> int:
    failures = violations()
    if failures:
        print("File-size guardrail failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("File-size guardrail passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
