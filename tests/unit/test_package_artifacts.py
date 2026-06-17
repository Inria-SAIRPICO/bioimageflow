"""Built package artifact contract tests."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest


ROOT = Path(__file__).parents[2]
TOOL_PACKAGE_NAMES = {
    "bioimageflow-common-tools",
    "bioimageflow-io-tools",
    "bioimageflow-measurement-tools",
    "bioimageflow-restoration-tools",
    "bioimageflow-sairpico-tools",
    "bioimageflow-segmentation-tools",
    "bioimageflow-spot-tools",
    "bioimageflow-tracking-tools",
}
FORBIDDEN_ARTIFACT_PARTS = {
    ".DS_Store",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
}


def _distribution_names() -> list[str]:
    return sorted(path.parent.name for path in (ROOT / "packages").glob("*/pyproject.toml"))


def _normalized_name(name: str) -> str:
    return name.replace("-", "_")


@pytest.fixture(scope="session")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("package-artifacts")
    result = subprocess.run(
        ["uv", "build", "--all-packages", "--out-dir", str(out_dir)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out_dir


def _wheel_path(out_dir: Path, distribution_name: str) -> Path:
    [path] = sorted(out_dir.glob(f"{_normalized_name(distribution_name)}-*.whl"))
    return path


def _sdist_path(out_dir: Path, distribution_name: str) -> Path:
    [path] = sorted(out_dir.glob(f"{_normalized_name(distribution_name)}-*.tar.gz"))
    return path


def _wheel_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _sdist_members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
    return [name.split("/", 1)[1] for name in names if "/" in name]


def _forbidden_members(members: list[str]) -> list[str]:
    offenders = []
    for member in members:
        parts = set(Path(member).parts)
        if parts.intersection(FORBIDDEN_ARTIFACT_PARTS):
            offenders.append(member)
    return sorted(offenders)


def test_wheels_exclude_docs_tests_and_generated_artifacts(built_artifacts: Path) -> None:
    offenders: dict[str, list[str]] = {}

    for distribution_name in _distribution_names():
        members = _wheel_members(_wheel_path(built_artifacts, distribution_name))
        unexpected = [
            member
            for member in members
            if member.startswith("docs/")
            or member.startswith("tests/")
            or member.startswith(f"{distribution_name}/docs/")
            or member.startswith(f"{distribution_name}/tests/")
        ]
        unexpected.extend(_forbidden_members(members))
        if unexpected:
            offenders[distribution_name] = sorted(unexpected)

    assert offenders == {}


def test_sdists_exclude_generated_artifacts_and_keep_tool_docs_tests(
    built_artifacts: Path,
) -> None:
    offenders: dict[str, list[str]] = {}

    for distribution_name in _distribution_names():
        members = _sdist_members(_sdist_path(built_artifacts, distribution_name))
        unexpected = _forbidden_members(members)
        if distribution_name in TOOL_PACKAGE_NAMES:
            if "docs/index.md" not in members:
                unexpected.append("missing docs/index.md")
            if not any(member.startswith("tests/") for member in members):
                unexpected.append("missing tests/")
        if unexpected:
            offenders[distribution_name] = sorted(unexpected)

    assert offenders == {}


def test_common_tools_data_file_is_in_wheel_and_sdist(built_artifacts: Path) -> None:
    distribution_name = "bioimageflow-common-tools"
    data_path = "bioimageflow_common_tools/data/blobs.txt"

    assert data_path in _wheel_members(_wheel_path(built_artifacts, distribution_name))
    assert data_path in _sdist_members(_sdist_path(built_artifacts, distribution_name))
