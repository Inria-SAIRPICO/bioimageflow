"""Orchestrator-side construction of strict worker tool origins."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from bioimageflow_core import (
    ArchiveModuleOriginV1,
    InstalledModuleOriginV1,
    ProcessingTool,
    SharedModuleOriginV1,
    SourceFileOriginV1,
    VersionedModuleOriginV1,
    WorkerToolOriginV1,
)


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _distribution_imports(distribution: importlib.metadata.Distribution) -> set[str]:
    declared = distribution.read_text("top_level.txt")
    if declared:
        return {
            line.strip()
            for line in declared.splitlines()
            if line.strip() and not line.startswith("#")
        }
    roots: set[str] = set()
    for file in distribution.files or ():
        first = file.parts[0] if file.parts else ""
        if first and not first.endswith((".dist-info", ".data")):
            roots.add(first.removesuffix(".py"))
    return roots


def _find_distribution(
    import_package: str, *, root: Path | None = None
) -> tuple[str, str]:
    if root is None:
        names = importlib.metadata.packages_distributions().get(import_package, [])
        candidates = []
        for name in names:
            try:
                candidates.append(
                    (_canonical_distribution(name), importlib.metadata.version(name))
                )
            except importlib.metadata.PackageNotFoundError:
                continue
    else:
        candidates = [
            (
                _canonical_distribution(distribution.metadata["Name"]),
                distribution.version,
            )
            for distribution in importlib.metadata.distributions(path=[str(root)])
            if import_package in _distribution_imports(distribution)
        ]
    unique = sorted(set(candidates))
    if len(unique) != 1:
        location = "the active environment" if root is None else str(root)
        raise ValueError(
            f"Cannot resolve exactly one installed distribution for import package "
            f"{import_package!r} in {location!r}."
        )
    return unique[0]


def _verify_declared_distribution(
    distribution_name: str,
    import_package: str,
    source_file: Path,
) -> str:
    try:
        distribution = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError(
            f"Worker distribution {distribution_name!r} is not installed."
        ) from exc
    actual_name = _canonical_distribution(distribution.metadata["Name"])
    if actual_name != distribution_name:
        raise ValueError(
            f"Worker distribution metadata names {actual_name!r}, not "
            f"{distribution_name!r}."
        )
    if import_package in _distribution_imports(distribution):
        return distribution.version

    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        parsed = json.loads(direct_url)
        url = parsed.get("url")
        if isinstance(url, str):
            location = urlparse(url)
            if location.scheme == "file":
                project_root = Path(unquote(location.path)).resolve(strict=True)
                try:
                    source_file.relative_to(project_root)
                except ValueError:
                    pass
                else:
                    return distribution.version
    raise ValueError(
        f"Distribution {distribution_name!r} does not provide import package "
        f"{import_package!r}."
    )


def _package_import_root(source_file: Path, module: str) -> Path | None:
    top_package = module.split(".", 1)[0]
    for parent in (source_file.parent, *source_file.parents):
        if parent.name == top_package and (parent / "__init__.py").is_file():
            return parent.parent.resolve()
    return None


def _versioned_store_root(source_file: Path, import_package: str, version: str) -> Path:
    for parent in (source_file.parent, *source_file.parents):
        if (
            parent.name == import_package
            and parent.parent.name == version
            and (parent / "__init__.py").is_file()
        ):
            return parent.parent.resolve()
    raise ValueError(
        f"Cannot locate the versioned store root for {import_package}=={version}."
    )


def resolve_worker_tool_origin(
    tool: ProcessingTool | type[ProcessingTool],
    *,
    installed_distribution: str | None = None,
) -> WorkerToolOriginV1:
    """Construct one complete verified worker origin for a processing tool."""
    tool_class = tool if isinstance(tool, type) else type(tool)
    if not issubclass(tool_class, ProcessingTool):
        raise TypeError("Worker origins can only be built for ProcessingTool classes.")
    source_file = Path(inspect.getsourcefile(tool_class) or inspect.getfile(tool_class))
    source_file = source_file.resolve(strict=True)
    class_name = tool_class.__name__
    canonical_module = getattr(
        tool_class, "_bif_canonical_module", tool_class.__module__
    )

    versioned_package = getattr(tool_class, "_bif_package", None)
    versioned_version = getattr(tool_class, "_bif_package_version", None)
    if isinstance(versioned_package, str) and isinstance(versioned_version, str):
        store_root = _versioned_store_root(
            source_file, versioned_package, versioned_version
        )
        distribution, installed_version = _find_distribution(
            versioned_package, root=store_root
        )
        if installed_version != versioned_version:
            raise ValueError(
                f"Versioned tool metadata expects {versioned_version!r}, but "
                f"distribution metadata declares {installed_version!r}."
            )
        return VersionedModuleOriginV1(
            distribution=distribution,
            import_package=versioned_package,
            version=versioned_version,
            canonical_module=canonical_module,
            scoped_module=tool_class.__module__,
            store_root=str(store_root),
            class_name=class_name,
        )

    source_id = getattr(tool_class, "_bif_custom_source_id", None)
    source_hash = getattr(tool_class, "_bif_custom_source_hash", None)
    worker_root = getattr(tool_class, "_bif_worker_sys_path", None)
    worker_module = getattr(tool_class, "_bif_worker_module", None)
    if (
        isinstance(source_id, str)
        and isinstance(source_hash, str)
        and isinstance(worker_root, str)
        and isinstance(worker_module, str)
    ):
        return ArchiveModuleOriginV1(
            source_id=source_id,
            source_hash=source_hash,
            canonical_module=canonical_module,
            scoped_module=worker_module,
            materialization_root=str(Path(worker_root).resolve(strict=True)),
            class_name=class_name,
        )

    declared_distribution = installed_distribution or getattr(
        tool_class, "_bif_worker_distribution", None
    )
    if declared_distribution is not None:
        if not isinstance(declared_distribution, str):
            raise TypeError("Worker distribution metadata must be a string.")
        canonical_distribution = _canonical_distribution(declared_distribution)
        if canonical_distribution != declared_distribution:
            raise ValueError(
                "Worker distribution metadata must use its canonical normalized spelling."
            )
        import_package = canonical_module.split(".", 1)[0]
        version = _verify_declared_distribution(
            declared_distribution,
            import_package,
            source_file,
        )
        return InstalledModuleOriginV1(
            distribution=declared_distribution,
            version=version,
            module=canonical_module,
            class_name=class_name,
        )

    import_root = _package_import_root(source_file, tool_class.__module__)
    if import_root is not None:
        return SharedModuleOriginV1(
            module=tool_class.__module__,
            import_root=str(import_root),
            source_hash=_file_hash(source_file),
            class_name=class_name,
        )
    return SourceFileOriginV1(
        path=str(source_file),
        source_hash=_file_hash(source_file),
        class_name=class_name,
    )
