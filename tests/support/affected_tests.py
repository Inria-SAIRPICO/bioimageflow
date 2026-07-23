"""Map changed paths to likely local validation commands.

The mapping is advisory and intentionally fails open to broader commands when a path is unknown.
It does not replace CI gates.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
import sys
from typing import TypedDict, cast

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from tests.support.ci_selectors import (
    ACCEPTANCE_TEST_COMMAND,
    CI_QUALITY_CONFIG_COMMAND,
    DIRECT_INTEGRATION_TEST_COMMAND,
    DOCS_BUILD_COMMAND,
    FAST_TEST_COMMAND,
    PACKAGE_METADATA_CONTRACTS_COMMAND,
    PACKAGE_TOOLS_TEST_COMMAND,
    UNIT_TEST_COMMAND,
)

ROOT = Path(__file__).resolve().parents[2]
OWNERSHIP_PATH = ROOT / "tests" / "ownership.toml"


class OwnershipArea(TypedDict):
    name: str
    sources: list[str]
    edit_tests: list[str]
    precommit_suites: list[str]


ADVISORY_NOTE = "Advisory only: run the required CI gates before merging; unknown paths fall back to broader tests."


def commands_for_paths(
    paths: Iterable[str],
    *,
    stage: str = "edit",
    root: Path = ROOT,
) -> list[str]:
    if stage == "merge":
        return [FAST_TEST_COMMAND]

    commands: list[str] = []
    seen: set[str] = set()
    normalized_paths = [_normalize_path(path) for path in paths if path.strip()]
    ownership = load_ownership(root / "tests" / "ownership.toml")

    if not normalized_paths:
        return [FAST_TEST_COMMAND]

    for path in normalized_paths:
        for command in _commands_for_path(
            path, stage=stage, root=root, ownership=ownership
        ):
            if command not in seen:
                commands.append(command)
                seen.add(command)

    return commands


def _normalize_path(path: str) -> PurePosixPath:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized)


def load_ownership(path: Path = OWNERSHIP_PATH) -> list[OwnershipArea]:
    """Load and minimally validate the development ownership map."""
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if data.get("version") != 1:
        raise ValueError(f"Unsupported ownership map version in {path}")
    areas = data.get("areas")
    if not isinstance(areas, list) or not areas:
        raise ValueError(f"Ownership map has no areas: {path}")
    for area in areas:
        if not isinstance(area, dict):
            raise ValueError(f"Invalid ownership area in {path}")
        if not all(
            isinstance(area.get(key), expected_type)
            for key, expected_type in (
                ("name", str),
                ("sources", list),
                ("edit_tests", list),
                ("precommit_suites", list),
            )
        ):
            raise ValueError(f"Incomplete ownership area in {path}: {area!r}")
    return cast(list[OwnershipArea], areas)


def matching_areas(
    path: PurePosixPath,
    ownership: Sequence[OwnershipArea],
) -> list[OwnershipArea]:
    path_text = path.as_posix()
    return [
        area
        for area in ownership
        if any(
            path_text == source or path_text.startswith(f"{source}/")
            for source in area["sources"]
        )
    ]


def unowned_platform_paths(
    *,
    root: Path = ROOT,
    ownership: Sequence[OwnershipArea] | None = None,
) -> list[str]:
    """Return orchestrator modules that have no affected-test owner."""
    areas = list(ownership or load_ownership(root / "tests" / "ownership.toml"))
    package_root = root / "packages" / "bioimageflow" / "bioimageflow"
    return [
        path.relative_to(root).as_posix()
        for path in sorted(package_root.rglob("*.py"))
        if not matching_areas(
            PurePosixPath(path.relative_to(root).as_posix()),
            areas,
        )
    ]


def _commands_for_path(
    path: PurePosixPath,
    *,
    stage: str,
    root: Path,
    ownership: Sequence[OwnershipArea],
) -> tuple[str, ...]:
    parts = path.parts
    path_text = path.as_posix()

    if path_text in {"pyproject.toml", "conftest.py"} or path_text.startswith(
        ".github/workflows/"
    ):
        return (CI_QUALITY_CONFIG_COMMAND, DOCS_BUILD_COMMAND)

    if _is_docs_path(parts):
        return (CI_QUALITY_CONFIG_COMMAND, DOCS_BUILD_COMMAND)

    if _is_example_workflow_path(parts):
        return (ACCEPTANCE_TEST_COMMAND, PACKAGE_TOOLS_TEST_COMMAND)

    areas = matching_areas(path, ownership)
    if areas:
        if stage == "precommit":
            suites = {suite for area in areas for suite in area["precommit_suites"]}
            return tuple(
                command
                for suite, command in (
                    ("unit", UNIT_TEST_COMMAND),
                    ("integration", DIRECT_INTEGRATION_TEST_COMMAND),
                )
                if suite in suites
            )
        return tuple(f"uv run pytest {' '.join(area['edit_tests'])}" for area in areas)

    if _is_package_path(parts):
        package_name = parts[1]
        if _is_core_package(parts):
            return (FAST_TEST_COMMAND,)
        return (
            f'uv run pytest packages/{package_name}/tests -m "not complete"',
            PACKAGE_METADATA_CONTRACTS_COMMAND,
        )

    if _is_test_path(parts):
        if stage == "edit" and path.suffix == ".py" and (root / path_text).is_file():
            return (f"uv run pytest {path_text}", FAST_TEST_COMMAND)
        if len(parts) > 1 and parts[1] == "unit":
            return (UNIT_TEST_COMMAND,)
        if len(parts) > 1 and parts[1] == "integration":
            return (DIRECT_INTEGRATION_TEST_COMMAND,)
        return (FAST_TEST_COMMAND,)

    return (FAST_TEST_COMMAND,)


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
    parser.add_argument(
        "paths", nargs="*", help="Changed paths relative to the repository root."
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read additional changed paths from standard input, one path per line.",
    )
    parser.add_argument(
        "--stage",
        choices=("edit", "precommit", "merge"),
        default="edit",
        help="Select the breadth of validation: focused edit loop, suite-level precommit, or full fast merge gate.",
    )
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if args.stdin:
        paths.extend(line.strip() for line in sys.stdin if line.strip())

    print(ADVISORY_NOTE)
    for command in commands_for_paths(paths, stage=args.stage):
        print(command)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
