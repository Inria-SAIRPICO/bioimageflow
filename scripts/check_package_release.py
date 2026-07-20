"""Validate a package-specific release tag and its optional artifacts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_support import (  # noqa: E402
    Package,
    ReleaseError,
    discover_packages,
    git_output,
    parse_release_tag,
    tag_exists,
    validate_release_artifacts,
)


@dataclass(frozen=True)
class ReleaseSelection:
    package: Package
    version: str
    tag: str


def validate_release(
    tag: str,
    *,
    root: Path = ROOT,
    artifact_dir: Path | None = None,
    require_clean: bool = True,
) -> ReleaseSelection:
    package, version = parse_release_tag(tag, discover_packages(root))
    if package.version != version:
        raise ReleaseError(
            f"Tag requests {package.name}=={version}, but pyproject.toml declares "
            f"{package.version}"
        )
    if not tag_exists(root, tag):
        raise ReleaseError(f"Release tag does not exist locally: {tag}")
    if git_output(root, "cat-file", "-t", tag) != "tag":
        raise ReleaseError(f"Release tag must be annotated: {tag}")
    if git_output(root, "rev-list", "-n", "1", tag) != git_output(root, "rev-parse", "HEAD"):
        raise ReleaseError(f"Release tag {tag} does not point to HEAD")
    if require_clean and git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Working tree must be clean before release validation")
    if artifact_dir is not None:
        validate_release_artifacts(artifact_dir, package, version)
    return ReleaseSelection(package=package, version=version, tag=tag)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag, for example bioimageflow-core-v0.1.7")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Also require exactly one matching wheel and source distribution",
    )
    parser.add_argument(
        "--field",
        choices=["package", "version", "directory", "tag"],
        help="Print only one validated release field",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow local changes; intended only for focused tooling tests",
    )
    args = parser.parse_args(argv)

    try:
        selection = validate_release(
            args.tag,
            artifact_dir=args.artifacts_dir,
            require_clean=not args.allow_dirty,
        )
    except ReleaseError as error:
        parser.error(str(error))

    fields = {
        "package": selection.package.name,
        "version": selection.version,
        "directory": str(selection.package.directory.relative_to(ROOT)),
        "tag": selection.tag,
    }
    if args.field:
        print(fields[args.field])
    else:
        print(f"Validated {selection.package.name}=={selection.version} from {selection.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
