"""Immutable laptop-side preparation for managed cluster deployments."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


from ._common import thaw_json
from .values import ClusterEnvironment, ParslConfiguration


DEPLOYMENT_MANIFEST_SCHEMA = "bioimageflow.cluster_deployment_manifest.v1"
MAX_BUNDLE_FILES = 2_048
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_LOGICAL_PATH = 512
MAX_PATH_DEPTH = 64


def _environment_failure(code: str, message: str) -> ValueError:
    return ValueError(f"{code}: {message}")


class _FrozenDict(dict[str, Any]):
    """JSON-encodable mapping that rejects in-place changes."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Prepared deployment metadata is immutable.")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable  # pyright: ignore[reportAssignmentType]
    setdefault = _immutable  # pyright: ignore[reportAssignmentType]
    update = _immutable  # pyright: ignore[reportAssignmentType]
    __ior__ = _immutable  # pyright: ignore[reportAssignmentType]


def _freeze_plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("Prepared deployment metadata requires string keys.")
        return _FrozenDict(
            {key: _freeze_plain_json(item) for key, item in value.items()}
        )
    if type(value) in {list, tuple}:
        return tuple(_freeze_plain_json(item) for item in value)
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise TypeError("Prepared deployment metadata must contain finite JSON values.")


def _environment_identity(environment: ClusterEnvironment) -> dict[str, Any]:
    """Return identity-bearing selections without laptop source paths."""
    return {
        "schema": environment.SCHEMA,
        "kind": environment.kind,
        "source": str(environment.source)
        if environment.kind == "existing_python"
        else None,
        "groups": list(environment.groups),
        "extras": list(environment.extras),
        "package": environment.package,
        "environment": environment.environment,
        "auth_refs": dict(environment.auth_refs),
    }


def _parsl_identity(configuration: ParslConfiguration) -> dict[str, Any]:
    """Return factory selections without laptop source paths."""
    return {
        "schema": configuration.SCHEMA,
        "source_kind": configuration.source_kind,
        "source": str(configuration.source)
        if configuration.source_kind == "module"
        else None,
        "factory": configuration.factory,
        "kwargs": thaw_json(configuration.kwargs),
        "secret_refs": dict(configuration.secret_refs),
        "include_count": len(configuration.include),
    }


def _copy_regular(
    source: Path,
    destination: Path,
    *,
    reject_hard_links: bool = False,
) -> int:
    before = source.stat(follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(
            f"Deployment source must be a regular non-symlink file: {source}."
        )
    if reject_hard_links and before.st_nlink != 1:
        raise ValueError(f"Deployment source must not be hard-linked: {source}.")
    if before.st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Deployment source exceeds the byte limit.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        with destination.open("xb") as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = source.stat(follow_symlinks=False)
    identities = {
        (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)
        for value in (before, opened, after, final)
    }
    if len(identities) != 1:
        raise ValueError(f"Deployment source changed while it was captured: {source}.")
    if reject_hard_links and final.st_nlink != 1:
        raise ValueError(f"Deployment source must not be hard-linked: {source}.")
    return final.st_size


def _copy_tree(source: Path, destination: Path) -> None:
    root_before = source.stat(follow_symlinks=False)
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise ValueError(
            f"Included Parsl package must be a non-symlink directory: {source}."
        )
    destination.mkdir(parents=True)
    seen_inodes: set[tuple[int, int]] = set()
    count = 0
    size = 0
    for child in sorted(source.rglob("*")):
        relative = child.relative_to(source)
        logical = relative.as_posix()
        if (
            len(relative.parts) > MAX_PATH_DEPTH
            or len(logical.encode("utf-8")) > MAX_LOGICAL_PATH
        ):
            raise ValueError("Included Parsl package exceeds the path-depth limit.")
        target = destination / relative
        metadata = child.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Included Parsl packages must not contain symlinks.")
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Included Parsl packages must contain regular files only.")
        identity = (metadata.st_dev, metadata.st_ino)
        if metadata.st_nlink != 1 or identity in seen_inodes:
            raise ValueError("Included Parsl packages must not contain hard links.")
        seen_inodes.add(identity)
        count += 1
        size += metadata.st_size
        if count > MAX_BUNDLE_FILES or size > MAX_BUNDLE_BYTES:
            raise ValueError("Included source package exceeds the bundle limits.")
        _copy_regular(child, target, reject_hard_links=True)
    root_after = source.stat(follow_symlinks=False)
    if (
        root_before.st_dev,
        root_before.st_ino,
        root_before.st_mode,
        root_before.st_mtime_ns,
    ) != (
        root_after.st_dev,
        root_after.st_ino,
        root_after.st_mode,
        root_after.st_mtime_ns,
    ):
        raise ValueError(f"Deployment source changed while it was captured: {source}.")


def _load_toml(path: Path, *, description: str) -> dict[str, Any]:
    try:
        import tomllib  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        try:
            import tomli as tomllib  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError as exc:
            raise ImportError(
                "Cluster environment preparation on Python 3.10 requires "
                "bioimageflow[cluster]."
            ) from exc
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{description} is not valid UTF-8 TOML.") from exc
    if type(value) is not dict:
        raise ValueError(f"{description} must contain a TOML table.")
    return value


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _stable_artifact(path: Path) -> dict[str, Any]:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise _environment_failure(
            "environment-artifact-missing",
            "A prepared distribution artifact is not a regular file.",
        )
    if metadata.st_size > MAX_BUNDLE_BYTES:
        raise _environment_failure(
            "environment-artifact-missing",
            "A prepared distribution artifact exceeds the supported limits.",
        )
    content = path.read_bytes()
    if len(content) != metadata.st_size or len(content) > MAX_BUNDLE_BYTES:
        raise _environment_failure(
            "environment-artifact-missing",
            "A prepared distribution artifact exceeds the supported limits.",
        )
    return {
        "filename": path.name,
        "size": len(content),
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
    }


def _uv_executable_and_version() -> tuple[str, str]:
    executable = shutil.which("uv")
    if executable is None:
        raise _environment_failure(
            "environment-installer-unavailable",
            "Locked uv preparation requires the uv executable.",
        )
    try:
        completed = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _environment_failure(
            "environment-installer-unavailable",
            "The uv executable could not be inspected.",
        ) from exc
    match = re.fullmatch(r"uv ([0-9]+(?:\.[0-9]+){1,3})(?: .*)?\n?", completed.stdout)
    if completed.returncode != 0 or match is None:
        raise _environment_failure(
            "environment-installer-unavailable",
            "The uv executable did not report a supported version.",
        )
    return executable, match.group(1)


def _verify_snapshot(source: Path, captured: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="bioimageflow-snapshot-verify-"
    ) as directory:
        comparison = Path(directory) / source.name
        _copy_regular(source, comparison)
        if comparison.read_bytes() != captured.read_bytes():
            raise _environment_failure(
                "environment-lock-invalid",
                "The uv project or lock changed during preparation.",
            )


def _require_directory(path: Path, *, description: str) -> None:
    metadata = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{description} must be a non-symlink directory.")


def _project_name(pyproject: Mapping[str, Any]) -> str | None:
    project = pyproject.get("project")
    if not isinstance(project, Mapping):
        return None
    name = project.get("name")
    return name if type(name) is str and name else None


def _package_directories(root: Path, pyproject: Mapping[str, Any]) -> tuple[Path, ...]:
    """Select common, build-metadata-declared package roots without copying a repository."""
    selected: list[Path] = []
    tool = pyproject.get("tool")
    if isinstance(tool, Mapping):
        hatch = tool.get("hatch")
        if isinstance(hatch, Mapping):
            build = hatch.get("build")
            targets = build.get("targets") if isinstance(build, Mapping) else None
            wheel = targets.get("wheel") if isinstance(targets, Mapping) else None
            packages = wheel.get("packages") if isinstance(wheel, Mapping) else None
            if isinstance(packages, list):
                selected.extend(root / item for item in packages if type(item) is str)
        setuptools = tool.get("setuptools")
        package_dir = (
            setuptools.get("package-dir") if isinstance(setuptools, Mapping) else None
        )
        if isinstance(package_dir, Mapping):
            selected.extend(
                root / item for item in package_dir.values() if type(item) is str
            )
        flit = tool.get("flit")
        module = flit.get("module") if isinstance(flit, Mapping) else None
        module_name = module.get("name") if isinstance(module, Mapping) else None
        if type(module_name) is str:
            selected.extend(
                (
                    root / module_name.replace(".", "/"),
                    root / "src" / module_name.replace(".", "/"),
                )
            )
    name = _project_name(pyproject)
    if name is not None:
        import_name = re.sub(r"[-_.]+", "_", name).lower()
        selected.extend((root / import_name, root / "src" / import_name))
    result: list[Path] = []
    for candidate in selected:
        if candidate.is_dir() and candidate not in result:
            result.append(candidate)
    return tuple(result)


def _capture_project_inputs(project_root: Path, destination: Path) -> None:
    _require_directory(project_root, description="A selected local project")
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.is_file():
        raise ValueError("A selected local project requires pyproject.toml.")
    captured_pyproject = destination / "pyproject.toml"
    _copy_regular(pyproject_path, captured_pyproject, reject_hard_links=True)
    pyproject = _load_toml(
        captured_pyproject, description="Local project pyproject.toml"
    )
    build_system = pyproject.get("build-system")
    if (
        not isinstance(build_system, Mapping)
        or type(build_system.get("build-backend")) is not str
    ):
        # Pure lock-only fixtures may have no importable local distribution.
        if _package_directories(project_root, pyproject):
            raise ValueError(
                "A packageable local project requires a declared build backend."
            )
        return
    for package_root in _package_directories(project_root, pyproject):
        relative = package_root.relative_to(project_root)
        _copy_tree(package_root, destination / relative)


def _wheel_filename(value: Mapping[str, Any]) -> str | None:
    name = value.get("name")
    if type(name) is str and name:
        return name
    url = value.get("url")
    if type(url) is str and url:
        return Path(unquote(urlparse(url).path)).name
    return None


def _expected_sha256(value: Mapping[str, Any]) -> str | None:
    hashes = value.get("hashes")
    if not isinstance(hashes, Mapping):
        return None
    digest = hashes.get("sha256")
    if type(digest) is not str:
        return None
    return digest.removeprefix("sha256:")


def _validate_wheelhouse_files(
    lock: Path,
    available: Mapping[str, Path],
) -> tuple[Path, ...]:
    """Validate the offline subset of a PEP 751 lock without target guessing."""
    try:
        from packaging.utils import canonicalize_name, parse_wheel_filename
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Wheelhouse deployment preparation requires bioimageflow[cluster]."
        ) from exc
    payload = _load_toml(lock, description="Wheelhouse pylock.toml")
    if type(payload.get("lock-version")) is not str:
        raise ValueError("An offline wheelhouse requires a PEP 751 lock-version.")
    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ValueError("An offline wheelhouse lock must contain packages.")
    selected: dict[str, Path] = {}
    for package in packages:
        if not isinstance(package, Mapping) or type(package.get("name")) is not str:
            raise ValueError("The wheelhouse lock contains an invalid package entry.")
        if (
            package.get("sdist") is not None
            or package.get("directory") is not None
            or package.get("vcs") is not None
        ):
            raise ValueError(
                "Offline wheelhouse locks must not select source distributions or mutable sources."
            )
        artifacts = package.get("wheels")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError(
                "Every wheelhouse package requires locked wheel artifacts."
            )
        matching: list[tuple[Path, Mapping[str, Any]]] = []
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise ValueError("The wheelhouse lock contains an invalid wheel entry.")
            filename = _wheel_filename(artifact)
            if filename is None:
                raise ValueError("Every locked wheel requires a filename or URL.")
            if filename in available:
                matching.append((available[filename], artifact))
        if len(matching) != 1:
            raise ValueError(
                "The wheelhouse must contain exactly one locked candidate per package."
            )
        wheel, artifact = matching[0]
        try:
            wheel_distribution = canonicalize_name(parse_wheel_filename(wheel.name)[0])
        except ValueError as exc:
            raise ValueError(f"Invalid wheel filename: {wheel.name}.") from exc
        if wheel_distribution != canonicalize_name(package["name"]):
            raise ValueError(
                f"Wheel {wheel.name!r} does not match its locked distribution."
            )
        expected_hash = _expected_sha256(artifact)
        if expected_hash is None or len(expected_hash) != 64:
            raise ValueError(f"Locked wheel {wheel.name!r} requires a SHA-256 hash.")
        content = wheel.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError(
                f"Wheel {wheel.name!r} does not match its locked SHA-256 hash."
            )
        size = artifact.get("size")
        if type(size) is not int or size != len(content):
            raise ValueError(f"Wheel {wheel.name!r} does not match its locked size.")
        selected[wheel.name] = wheel
    extras = set(available) - set(selected)
    if extras:
        raise ValueError(f"The wheelhouse contains unlocked wheels: {sorted(extras)}.")
    return tuple(selected[name] for name in sorted(selected))


def _environment_files(environment: ClusterEnvironment) -> tuple[Path, ...]:
    if environment.kind == "existing_python":
        return ()
    source = Path(environment.source)
    if environment.kind == "uv":
        try:
            _require_directory(source, description="A uv project")
        except (OSError, ValueError) as exc:
            raise _environment_failure(
                "environment-lock-invalid", "The uv project root is unavailable."
            ) from exc
        pyproject = source / "pyproject.toml"
        lock = source / "uv.lock"
        if not pyproject.is_file() or not lock.is_file():
            raise _environment_failure(
                "environment-lock-invalid",
                "A uv environment requires pyproject.toml and uv.lock.",
            )
        return pyproject, lock
    if environment.kind == "pixi":
        _require_directory(source, description="A Pixi project")
        manifest = source / "pixi.toml"
        if not manifest.is_file():
            candidate = source / "pyproject.toml"
            if not candidate.is_file():
                raise ValueError(
                    "A Pixi environment requires pixi.toml or pyproject.toml."
                )
            manifest = candidate
        lock = source / "pixi.lock"
        if not lock.is_file():
            raise ValueError("A Pixi environment requires pixi.lock.")
        return manifest, lock
    if environment.kind == "pylock":
        lock = environment.lock or source
        if not lock.is_file() or lock.name != "pylock.toml":
            raise ValueError("A standard Python environment requires pylock.toml.")
        files = [lock]
        if environment.project is not None:
            project = (
                environment.project / "pyproject.toml"
                if environment.project.is_dir()
                else environment.project
            )
            if not project.is_file():
                raise ValueError("The selected pylock project has no pyproject.toml.")
            files.append(project)
        return tuple(files)
    if environment.kind == "wheelhouse":
        lock = environment.lock
        if lock is None or not lock.is_file():
            raise ValueError("An offline wheelhouse requires an existing lock file.")
        _require_directory(source, description="The wheelhouse")
        wheels = tuple(sorted(source.glob("*.whl")))
        if not wheels:
            raise ValueError("The wheelhouse contains no wheels.")
        if any(path.is_symlink() or not path.is_file() for path in wheels):
            raise ValueError("The wheelhouse must contain regular non-symlink wheels.")
        return (lock, *wheels)
    raise ValueError("Unsupported cluster environment kind.")


__all__ = [
    "MAX_BUNDLE_BYTES",
    "MAX_BUNDLE_FILES",
    "MAX_LOGICAL_PATH",
    "MAX_PATH_DEPTH",
    "_capture_project_inputs",
    "_copy_regular",
    "_copy_tree",
    "_environment_failure",
    "_environment_files",
    "_load_toml",
    "_require_directory",
    "_stable_artifact",
    "_uv_executable_and_version",
    "_validate_wheelhouse_files",
    "_verify_snapshot",
]
