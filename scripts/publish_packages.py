"""Plan or perform a one-time local batch publication to PyPI."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Literal

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_support import (  # noqa: E402
    Package,
    ReleaseError,
    discover_packages,
    git_output,
    pypi_version,
    validate_release_artifacts,
)


PYPI_SIMPLE_URL = "https://pypi.org/simple"
PYPI_UPLOAD_URL = "https://upload.pypi.org/legacy/"
DecisionAction = Literal["publish", "skip", "error"]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PublishDecision:
    """One distribution's action in a local batch publication."""

    package: Package
    pypi_version: str | None
    action: DecisionAction
    detail: str


def validate_target_version(version_text: str) -> str:
    """Require the same stable three-part version syntax as release tags."""
    try:
        version = Version(version_text)
    except InvalidVersion as error:
        raise ReleaseError(f"Invalid target version {version_text!r}") from error
    if (
        version.public != version_text
        or len(version.release) != 3
        or version.is_prerelease
        or version.is_devrelease
        or version.is_postrelease
    ):
        raise ReleaseError(
            f"Invalid target version {version_text!r}; expected <major>.<minor>.<patch>"
        )
    return version_text


def select_packages(
    packages: Sequence[Package],
    included_names: Sequence[str] | None,
    excluded_names: Sequence[str],
) -> list[Package]:
    """Apply optional include/exclude filters while rejecting unknown names."""
    packages_by_name = {package.name: package for package in packages}
    requested = set(included_names or packages_by_name)
    excluded = set(excluded_names)
    unknown = (requested | excluded) - packages_by_name.keys()
    if unknown:
        raise ReleaseError(f"Unknown package name(s): {', '.join(sorted(unknown))}")
    if included_names and excluded:
        raise ReleaseError("Use either --package or --exclude-package, not both")
    return [
        package
        for package in packages
        if package.name in requested and package.name not in excluded
    ]


def build_publish_plan(
    target_version: str,
    packages: Sequence[Package],
    remote_versions: Mapping[str, str | None],
) -> list[PublishDecision]:
    """Decide which packages need publication without mutating local state."""
    target_text = validate_target_version(target_version)
    target = Version(target_text)
    decisions = []
    for package in packages:
        remote_text = remote_versions[package.name]
        try:
            remote = Version(remote_text) if remote_text is not None else None
            local = Version(package.version)
        except InvalidVersion as error:
            raise ReleaseError(f"Invalid version for {package.name}: {error}") from error

        if remote is not None and remote >= target:
            relation = "already has" if remote == target else "is newer than"
            decisions.append(
                PublishDecision(
                    package,
                    remote_text,
                    "skip",
                    f"PyPI {relation} the requested version",
                )
            )
        elif local != target:
            decisions.append(
                PublishDecision(
                    package,
                    remote_text,
                    "error",
                    f"local version is {package.version}, expected {target_text}",
                )
            )
        else:
            detail = "project is unpublished" if remote is None else f"PyPI has {remote_text}"
            decisions.append(PublishDecision(package, remote_text, "publish", detail))
    return decisions


def fetch_remote_versions(packages: Sequence[Package]) -> dict[str, str | None]:
    """Query PyPI, failing closed when any project status cannot be determined."""
    versions = {}
    for package in packages:
        try:
            versions[package.name] = pypi_version(package.name)
        except Exception as error:
            raise ReleaseError(f"Could not query PyPI for {package.name}: {error}") from error
    return versions


def publish_batch(
    decisions: Sequence[PublishDecision],
    *,
    root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Build, validate, and publish every planned distribution sequentially."""
    errors = [decision for decision in decisions if decision.action == "error"]
    if errors:
        raise ReleaseError("Resolve all plan errors before publishing")

    selected = [decision for decision in decisions if decision.action == "publish"]
    if not selected:
        return []

    environment = dict(os.environ if environ is None else environ)
    if not environment.get("UV_PUBLISH_TOKEN"):
        raise ReleaseError(
            "UV_PUBLISH_TOKEN is required; use a temporary account-scoped PyPI token"
        )
    if git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Working tree must be clean before publishing")

    run = subprocess.run if runner is None else runner
    build_environment = dict(environment)
    build_environment.pop("UV_PUBLISH_TOKEN", None)
    published = []
    with tempfile.TemporaryDirectory(prefix="bioimageflow-publish-") as temporary:
        artifact_root = Path(temporary)
        for decision in selected:
            package = decision.package
            artifact_dir = artifact_root / package.name
            try:
                run(
                    [
                        "uv",
                        "build",
                        "--package",
                        package.name,
                        "--no-sources",
                        "--clear",
                        "--out-dir",
                        str(artifact_dir),
                    ],
                    cwd=root,
                    check=True,
                    env=build_environment,
                    text=True,
                )
                artifacts = validate_release_artifacts(
                    artifact_dir,
                    package,
                    package.version,
                )
                run(
                    [
                        "uv",
                        "publish",
                        "--trusted-publishing",
                        "never",
                        "--no-attestations",
                        "--publish-url",
                        PYPI_UPLOAD_URL,
                        "--check-url",
                        PYPI_SIMPLE_URL,
                        *(str(path) for path in artifacts),
                    ],
                    cwd=root,
                    check=True,
                    env=environment,
                    text=True,
                )
            except subprocess.CalledProcessError as error:
                raise ReleaseError(
                    f"Publication stopped at {package.name}; command exited with "
                    f"status {error.returncode}"
                ) from error
            published.append(package.name)
    return published


def _print_plan(decisions: Sequence[PublishDecision]) -> None:
    headers = ("PACKAGE", "LOCAL", "PYPI", "ACTION", "DETAIL")
    rows = [
        (
            decision.package.name,
            decision.package.version,
            decision.pypi_version or "-",
            decision.action,
            decision.detail,
        )
        for decision in decisions
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "publish"])
    parser.add_argument("version", help="Stable target version, for example 0.1.6")
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        help="Include only this package; repeat to select a batch subset",
    )
    parser.add_argument(
        "--exclude-package",
        action="append",
        default=[],
        help="Exclude this package; repeat as needed",
    )
    args = parser.parse_args(argv)

    try:
        packages = select_packages(
            discover_packages(),
            args.packages,
            args.exclude_package,
        )
        decisions = build_publish_plan(
            args.version,
            packages,
            fetch_remote_versions(packages),
        )
        _print_plan(decisions)
        if any(decision.action == "error" for decision in decisions):
            return 1
        if args.command == "plan":
            return 0
        published = publish_batch(decisions)
    except ReleaseError as error:
        parser.error(str(error))

    if published:
        print(f"Published {len(published)} package(s): {', '.join(published)}")
    else:
        print("Nothing to publish; every selected project is already at or above the target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
