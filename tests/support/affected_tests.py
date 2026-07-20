"""Map changed paths to likely local validation commands.

The mapping is advisory and intentionally fails open to broader commands when a path is unknown.
It does not replace CI gates.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
import sys

from tests.support.ci_selectors import (
    ACCEPTANCE_TEST_COMMAND,
    CI_QUALITY_CONFIG_COMMAND,
    DEFAULT_TEST_COMMAND,
    DOCS_BUILD_COMMAND,
    FAST_TEST_COMMAND,
    PACKAGE_METADATA_CONTRACTS_COMMAND,
    PACKAGE_TOOLS_TEST_COMMAND,
)

ADVISORY_NOTE = (
    "Advisory only: run the required CI gates before merging; unknown paths fall back to broader tests."
)


def commands_for_paths(paths: Iterable[str]) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    normalized_paths = [_normalize_path(path) for path in paths if path.strip()]

    if not normalized_paths:
        return [DEFAULT_TEST_COMMAND]

    for path in normalized_paths:
        for command in _commands_for_path(path):
            if command not in seen:
                commands.append(command)
                seen.add(command)

    return commands


def _normalize_path(path: str) -> PurePosixPath:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized)


def _commands_for_path(path: PurePosixPath) -> tuple[str, ...]:
    parts = path.parts
    path_text = path.as_posix()

    if (
        path_text in {"pyproject.toml", "conftest.py"}
        or path_text.startswith(".github/workflows/")
    ):
        return (CI_QUALITY_CONFIG_COMMAND, DOCS_BUILD_COMMAND)

    if _is_docs_path(parts):
        return (CI_QUALITY_CONFIG_COMMAND, DOCS_BUILD_COMMAND)

    if _is_example_workflow_path(parts):
        return (ACCEPTANCE_TEST_COMMAND, PACKAGE_TOOLS_TEST_COMMAND)

    if _is_package_path(parts):
        package_name = parts[1]
        if _is_core_package(parts):
            return (FAST_TEST_COMMAND,)
        return (
            f'uv run pytest packages/{package_name}/tests -m "not complete"',
            PACKAGE_METADATA_CONTRACTS_COMMAND,
        )

    if _is_test_path(parts):
        if path.suffix == ".py" and path_text.startswith("tests/"):
            return (f"uv run pytest {path_text}", FAST_TEST_COMMAND)
        return (FAST_TEST_COMMAND,)

    return (DEFAULT_TEST_COMMAND,)


def _is_docs_path(parts: Sequence[str]) -> bool:
    return bool(parts) and parts[0] in {"docs", "README.md"}


def _is_example_workflow_path(parts: Sequence[str]) -> bool:
    return bool(parts) and parts[0] == "example_workflows"


def _is_test_path(parts: Sequence[str]) -> bool:
    return bool(parts) and parts[0] == "tests"


def _is_package_path(parts: Sequence[str]) -> bool:
    return len(parts) >= 2 and parts[0] == "packages"


def _is_core_package(parts: Sequence[str]) -> bool:
    if len(parts) < 2:
        return False
    package_name = parts[1]
    return package_name in {"bioimageflow", "bioimageflow-core"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print likely local validation commands for changed paths."
    )
    parser.add_argument("paths", nargs="*", help="Changed paths relative to the repository root.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read additional changed paths from standard input, one path per line.",
    )
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if args.stdin:
        paths.extend(line.strip() for line in sys.stdin if line.strip())

    print(ADVISORY_NOTE)
    for command in commands_for_paths(paths):
        print(command)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
