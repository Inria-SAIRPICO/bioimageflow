"""Release tooling contracts for independently versioned packages."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest
import yaml

from scripts.check_package_release import validate_release
from scripts.package_status import package_status
from scripts.release_support import (
    Package,
    ReleaseError,
    discover_packages,
    parse_release_tag,
    validate_release_artifacts,
)


def _run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, check=True, capture_output=True, text=True)


def _create_release_repository(tmp_path: Path) -> tuple[Path, Package]:
    root = tmp_path / "repository"
    package_dir = root / "packages" / "demo-tools"
    package_dir.mkdir(parents=True)
    (package_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-tools"\nversion = "1.2.3"\n'
    )
    _run(root, "git", "init")
    _run(root, "git", "config", "user.email", "release@example.invalid")
    _run(root, "git", "config", "user.name", "Release Test")
    _run(root, "git", "add", "packages/demo-tools/pyproject.toml")
    _run(root, "git", "commit", "-m", "release demo-tools")
    _run(root, "git", "tag", "-a", "demo-tools-v1.2.3", "-m", "Release 1.2.3")
    [package] = discover_packages(root)
    return root, package


def _write_artifacts(artifact_dir: Path, package: Package) -> None:
    artifact_dir.mkdir()
    metadata = (
        f"Metadata-Version: 2.1\nName: {package.name}\nVersion: {package.version}\n\n"
    ).encode()
    wheel = artifact_dir / f"{package.normalized_name}-{package.version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{package.normalized_name}-{package.version}.dist-info/METADATA",
            metadata,
        )
    sdist = artifact_dir / f"{package.normalized_name}-{package.version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"{package.name}-{package.version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_workspace_packages_are_discovered_independently() -> None:
    packages = discover_packages()

    assert len(packages) == 10
    assert {package.name for package in packages} == {
        "bioimageflow",
        "bioimageflow-common-tools",
        "bioimageflow-core",
        "bioimageflow-io-tools",
        "bioimageflow-measurement-tools",
        "bioimageflow-restoration-tools",
        "bioimageflow-sairpico-tools",
        "bioimageflow-segmentation-tools",
        "bioimageflow-spot-tools",
        "bioimageflow-tracking-tools",
    }


def test_release_tag_selects_exact_package_and_version() -> None:
    packages = discover_packages()

    package, version = parse_release_tag("bioimageflow-core-v0.1.6", packages)

    assert package.name == "bioimageflow-core"
    assert version == "0.1.6"


@pytest.mark.parametrize(
    "tag",
    [
        "v0.1.6",
        "bioimageflow-core-0.1.6",
        "bioimageflow-core-v0.1",
        "unknown-package-v0.1.6",
    ],
)
def test_invalid_release_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ReleaseError, match="Invalid release tag"):
        parse_release_tag(tag, discover_packages())


def test_release_validation_requires_matching_annotated_tag_at_head(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)

    selection = validate_release(package.release_tag, root=root)

    assert selection.package == package
    assert selection.version == "1.2.3"


def test_release_validation_rejects_changes_after_tag(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)
    (package.directory / "module.py").write_text("value = 1\n")

    with pytest.raises(ReleaseError, match="Working tree must be clean"):
        validate_release(package.release_tag, root=root)


def test_release_artifacts_must_be_exactly_one_matching_pair(tmp_path: Path) -> None:
    _, package = _create_release_repository(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    _write_artifacts(artifact_dir, package)

    artifacts = validate_release_artifacts(artifact_dir, package, package.version)

    assert len(artifacts) == 2


def test_release_artifacts_reject_another_distribution(tmp_path: Path) -> None:
    _, package = _create_release_repository(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    _write_artifacts(artifact_dir, package)
    (artifact_dir / "other-1.0.0-py3-none-any.whl").write_bytes(b"not a wheel")

    with pytest.raises(ReleaseError, match="exactly one wheel"):
        validate_release_artifacts(artifact_dir, package, package.version)


def test_status_uses_package_specific_tag_to_detect_changes(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)

    assert package_status(package, "1.2.3", root)[0] == "up-to-date"

    (package.directory / "module.py").write_text("value = 1\n")
    assert package_status(package, "1.2.3", root)[0] == "bump-required"


def test_status_distinguishes_unpublished_pending_and_behind(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)

    assert package_status(package, None, root)[0] == "unpublished"
    assert package_status(package, "1.2.2", root)[0] == "pending"
    assert package_status(package, "1.2.4", root)[0] == "behind"


def test_gitlab_release_job_is_manual_tag_scoped_and_token_protected() -> None:
    root = Path(__file__).parents[2]
    config = yaml.safe_load((root / ".gitlab-ci.yml").read_text())
    job = config["release:pypi"]
    script = "\n".join(job["script"])

    assert job["stage"] == "release"
    assert job["resource_group"] == "pypi"
    assert job["environment"] == {"name": "pypi"}
    assert job["rules"][0]["when"] == "manual"
    assert "CI_COMMIT_TAG" in job["rules"][0]["if"]
    assert "UV_PUBLISH_TOKEN" in script
    assert "--package \"$RELEASE_PACKAGE\"" in script
    assert "--trusted-publishing never" in script
    assert "dist/release/*" in script
    assert "id_tokens" not in job
