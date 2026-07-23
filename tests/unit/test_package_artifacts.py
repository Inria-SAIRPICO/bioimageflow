"""Built package artifact contract tests."""

from __future__ import annotations

from collections.abc import Mapping
from email.parser import BytesParser
from email.policy import default
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest
from packaging.requirements import Requirement


pytestmark = pytest.mark.packaging


ROOT = Path(__file__).parents[2]
PREBUILT_ARTIFACTS_ENV = "BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR"
SELECTED_PACKAGE_ENV = "BIOIMAGEFLOW_PACKAGE_ARTIFACTS_PACKAGE"
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
PUBLIC_IMPORT_MODULES = {
    "bioimageflow": "bioimageflow",
    "bioimageflow-common-tools": "bioimageflow_common_tools",
    "bioimageflow-core": "bioimageflow_core",
    "bioimageflow-io-tools": "bioimageflow_io_tools",
    "bioimageflow-measurement-tools": "bioimageflow_measurement_tools",
    "bioimageflow-restoration-tools": "bioimageflow_restoration_tools",
    "bioimageflow-sairpico-tools": "bioimageflow_sairpico_tools",
    "bioimageflow-segmentation-tools": "bioimageflow_segmentation_tools",
    "bioimageflow-spot-tools": "bioimageflow_spot_tools",
    "bioimageflow-tracking-tools": "bioimageflow_tracking_tools",
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


def _distribution_names(environ: Mapping[str, str] = os.environ) -> list[str]:
    names = sorted(path.parent.name for path in (ROOT / "packages").glob("*/pyproject.toml"))
    selected = environ.get(SELECTED_PACKAGE_ENV)
    if selected is None:
        return names
    if selected not in names:
        raise ValueError(f"Unknown package selected through {SELECTED_PACKAGE_ENV}: {selected}")
    return [selected]


def _normalized_name(name: str) -> str:
    return name.replace("-", "_")


def _artifact_dir_from_env(environ: Mapping[str, str] = os.environ) -> Path | None:
    value = environ.get(PREBUILT_ARTIFACTS_ENV)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _assert_expected_artifacts_exist(out_dir: Path) -> None:
    missing = []
    for distribution_name in _distribution_names():
        normalized = _normalized_name(distribution_name)
        if not list(out_dir.glob(f"{normalized}-*.whl")):
            missing.append(f"{normalized}-*.whl")
        if not list(out_dir.glob(f"{normalized}-*.tar.gz")):
            missing.append(f"{normalized}-*.tar.gz")
    assert missing == []


@pytest.fixture(scope="session")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    prebuilt_artifacts_dir = _artifact_dir_from_env()
    if prebuilt_artifacts_dir is not None:
        assert prebuilt_artifacts_dir.is_dir()
        _assert_expected_artifacts_exist(prebuilt_artifacts_dir)
        return prebuilt_artifacts_dir

    out_dir = tmp_path_factory.mktemp("package-artifacts")
    distribution_names = _distribution_names()
    package_arguments = (
        ["--package", distribution_names[0]]
        if len(distribution_names) == 1
        else ["--all-packages"]
    )
    result = subprocess.run(
        ["uv", "build", *package_arguments, "--out-dir", str(out_dir)],
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


def _wheel_metadata(path: Path):
    with zipfile.ZipFile(path) as archive:
        [metadata_name] = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        return BytesParser(policy=default).parsebytes(archive.read(metadata_name))


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


def test_relative_prebuilt_artifact_dir_resolves_from_repo_root() -> None:
    assert _artifact_dir_from_env({PREBUILT_ARTIFACTS_ENV: "dist/packages"}) == (
        ROOT / "dist/packages"
    )


def test_missing_prebuilt_artifact_dir_env_keeps_local_build_fallback() -> None:
    assert _artifact_dir_from_env({}) is None


def test_selected_package_limits_artifact_contract() -> None:
    assert _distribution_names({SELECTED_PACKAGE_ENV: "bioimageflow-core"}) == [
        "bioimageflow-core"
    ]


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


def test_spot_tools_atlas_data_file_is_in_wheel_and_sdist(
    request: pytest.FixtureRequest,
) -> None:
    distribution_name = "bioimageflow-spot-tools"
    if distribution_name not in _distribution_names():
        pytest.skip(f"{distribution_name} is not the selected release package")
    built_artifacts = request.getfixturevalue("built_artifacts")
    assert isinstance(built_artifacts, Path)
    data_path = "bioimageflow_spot_tools/data/blobs.txt"

    assert data_path in _wheel_members(_wheel_path(built_artifacts, distribution_name))
    assert data_path in _sdist_members(_sdist_path(built_artifacts, distribution_name))


def test_built_wheels_import_public_modules(
    built_artifacts: Path,
    tmp_path: Path,
) -> None:
    distribution_names = _distribution_names()
    wheel_paths = {
        distribution_name: _wheel_path(built_artifacts, distribution_name)
        for distribution_name in distribution_names
    }
    public_import_modules = {
        name: module
        for name, module in PUBLIC_IMPORT_MODULES.items()
        if name in distribution_names
    }
    code = """
from pathlib import Path
import importlib
import importlib.abc
import json
import sys

class BlockParsl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "parsl" or fullname.startswith("parsl."):
            raise AssertionError(f"base wheel imported optional Parsl: {fullname}")
        return None

sys.meta_path.insert(0, BlockParsl())

modules = json.loads(sys.argv[1])
wheel_paths = json.loads(sys.argv[2])
failures = {}

for distribution_name, module_name in modules.items():
    module = importlib.import_module(module_name)
    module_file = str(Path(module.__file__).resolve())
    wheel_path = wheel_paths[distribution_name]
    if wheel_path not in module_file:
        failures[module_name] = module_file

if failures:
    raise SystemExit(json.dumps(failures, sort_keys=True))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in wheel_paths.values())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            json.dumps(public_import_modules, sort_keys=True),
            json.dumps(
                {name: str(path) for name, path in wheel_paths.items()},
                sort_keys=True,
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_orchestrator_wheel_declares_bounded_parsl_extra(
    built_artifacts: Path,
) -> None:
    if "bioimageflow" not in _distribution_names():
        pytest.skip("bioimageflow is not the selected release package")

    metadata = _wheel_metadata(_wheel_path(built_artifacts, "bioimageflow"))
    requirements = metadata.get_all("Requires-Dist") or []
    parsl_requirements = [
        Requirement(requirement)
        for requirement in requirements
        if Requirement(requirement).name == "parsl"
    ]

    assert metadata.get_all("Provides-Extra") == ["parsl"]
    assert len(parsl_requirements) == 1
    [parsl_requirement] = parsl_requirements
    assert str(parsl_requirement.specifier) == "<2026.6,>=2026.5.25"
    assert str(parsl_requirement.marker) == 'extra == "parsl"'
