"""Shared helpers for independently versioned package releases."""

from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
import json
from pathlib import Path
import subprocess
import tarfile
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
PYPI_JSON_BASE_URL = "https://pypi.org/pypi"


class ReleaseError(RuntimeError):
    """Raised when a package release invariant is not satisfied."""


@dataclass(frozen=True)
class Package:
    """A publishable workspace package."""

    name: str
    version: str
    directory: Path

    @property
    def normalized_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def release_tag(self) -> str:
        return f"{self.name}-v{self.version}"


def load_pyproject(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def discover_packages(root: Path = ROOT) -> list[Package]:
    packages = []
    for pyproject_path in sorted((root / "packages").glob("*/pyproject.toml")):
        project = load_pyproject(pyproject_path)["project"]
        package = Package(
            name=project["name"],
            version=project["version"],
            directory=pyproject_path.parent,
        )
        if package.directory.name != package.name:
            raise ReleaseError(
                f"Package directory {package.directory.name!r} does not match "
                f"project name {package.name!r}"
            )
        packages.append(package)
    if not packages:
        raise ReleaseError(f"No packages found below {root / 'packages'}")
    return packages


def parse_release_tag(tag: str, packages: list[Package]) -> tuple[Package, str]:
    for package in sorted(packages, key=lambda item: len(item.name), reverse=True):
        prefix = f"{package.name}-v"
        if not tag.startswith(prefix):
            continue
        version_text = tag.removeprefix(prefix)
        try:
            version = Version(version_text)
        except InvalidVersion:
            break
        if (
            not version_text
            or version.public != version_text
            or version.is_prerelease
            or version.is_devrelease
            or version.is_postrelease
        ):
            break
        if len(version.release) != 3:
            break
        return package, version_text
    expected = "<package>-v<major>.<minor>.<patch>"
    raise ReleaseError(f"Invalid release tag {tag!r}; expected {expected}")


def git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleaseError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def tag_exists(root: Path, tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def package_changed_since_tag(root: Path, package: Package, tag: str) -> bool:
    relative_directory = str(package.directory.relative_to(root))
    result = subprocess.run(
        ["git", "diff", "--quiet", tag, "--", relative_directory],
        cwd=root,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise ReleaseError(f"Could not compare {package.name} with tag {tag}")
    if result.returncode == 1:
        return True
    untracked = git_output(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        relative_directory,
    )
    return bool(untracked)


def pypi_version(
    package_name: str,
    *,
    base_url: str = PYPI_JSON_BASE_URL,
    timeout: float = 10.0,
) -> str | None:
    request = Request(
        f"{base_url.rstrip('/')}/{package_name}/json",
        headers={"User-Agent": "bioimageflow-package-status/1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    return str(payload["info"]["version"])


def validate_release_artifacts(
    artifact_dir: Path,
    package: Package,
    version: str,
) -> list[Path]:
    if not artifact_dir.is_dir():
        raise ReleaseError(f"Artifact directory does not exist: {artifact_dir}")

    artifacts = sorted(
        path
        for path in artifact_dir.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
        raise ReleaseError(
            "Release directory must contain exactly one wheel and one source distribution; "
            f"found {[path.name for path in artifacts]}"
        )

    expected_name = canonicalize_name(package.name)
    metadata = [
        _wheel_metadata(wheels[0]),
        _sdist_metadata(sdists[0]),
    ]
    for artifact, artifact_metadata in zip([wheels[0], sdists[0]], metadata, strict=True):
        actual_name = canonicalize_name(str(artifact_metadata["Name"]))
        actual_version = str(artifact_metadata["Version"])
        if actual_name != expected_name or actual_version != version:
            raise ReleaseError(
                f"Unexpected metadata in {artifact.name}: "
                f"{actual_name}=={actual_version}, expected {expected_name}=={version}"
            )
    return artifacts


def _wheel_metadata(path: Path):
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ReleaseError(f"Expected one METADATA file in {path.name}")
        return BytesParser(policy=email_policy).parsebytes(archive.read(metadata_names[0]))


def _sdist_metadata(path: Path):
    with tarfile.open(path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")]
        if len(members) != 1:
            raise ReleaseError(f"Expected one PKG-INFO file in {path.name}")
        extracted = archive.extractfile(members[0])
        if extracted is None:
            raise ReleaseError(f"Could not read PKG-INFO from {path.name}")
        return BytesParser(policy=email_policy).parsebytes(extracted.read())
