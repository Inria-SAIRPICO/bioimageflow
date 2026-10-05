"""Orchestrator-side construction of strict worker tool origins."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping
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
    _captured_source_hash: str | None = None,
) -> WorkerToolOriginV1:
    """Construct one complete verified worker origin for a processing tool."""
    tool_class = tool if isinstance(tool, type) else type(tool)
    if not issubclass(tool_class, ProcessingTool):
        raise TypeError("Worker origins can only be built for ProcessingTool classes.")
    source_file = Path(getattr(tool_class, "_bif_admitted_source_file", None) or inspect.getsourcefile(tool_class) or inspect.getfile(tool_class))
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
            source_hash=_captured_source_hash or _file_hash(source_file),
            class_name=class_name,
        )
    return SourceFileOriginV1(
        path=str(source_file),
        source_hash=_captured_source_hash or _file_hash(source_file),
        class_name=class_name,
    )


@dataclass(frozen=True)
class ExecutableCapture:
    """One controller admission shared by identity, provenance and dispatch."""

    scientific_key: Mapping[str, Any]
    callbacks: Mapping[str, Callable[..., Any]]
    worker_origin: WorkerToolOriginV1 | None
    qualification: tuple[str, ...]


def _declaration_identity(tool: Any) -> dict[str, Any]:
    """Reuse the captured facade's portable contract, without copying defaults."""
    from bioimageflow.dataframe_tool import Passthrough
    from bioimageflow.validation.schema import extract_image_spec, serialize_image_spec
    from bioimageflow.validation.serialization import _is_nullable
    from bioimageflow.validation.type_descriptors import encode_annotation
    from bioimageflow_core.types import annotation_metadata, extract_gui_meta

    def constraints(annotation: Any) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        gui = extract_gui_meta(annotation)
        if gui is not None:
            facts.update({name: getattr(gui, name) for name in ("min", "max") if getattr(gui, name) is not None})
        metadata = annotation_metadata(annotation)
        for item in metadata:
            if type(item).__module__ == "pydantic.fields":
                items = item.metadata
            else:
                items = (item,)
            for constraint in items:
                if type(constraint).__module__ != "annotated_types":
                    continue
                for name in ("gt", "ge", "lt", "le", "multiple_of", "min_length", "max_length"):
                    if hasattr(constraint, name):
                        facts[name] = getattr(constraint, name)
        return facts

    def model_identity(model: Any) -> Any:
        if model is None:
            return None
        return {
            "passthrough": issubclass(model, Passthrough),
            "field_names": list(model._get_all_annotations()),
            "fields": {
                name: {"type_spec": encode_annotation(annotation),
                       "required": not hasattr(model, name),
                       "nullable": _is_nullable(annotation),
                       "constraints": constraints(annotation),
                       "image_spec": serialize_image_spec(extract_image_spec(annotation))}
                for name, annotation in model._get_all_annotations().items()
            },
        }
    return {"inputs": model_identity(tool.Inputs), "outputs": model_identity(tool.Outputs)}


def capture_tool_executable(
    tool: Any, *, managed: bool, canonicalize: Callable[[Any], str],
    declared_versions: dict[str, tuple[str, str] | None] | None = None,
) -> ExecutableCapture:
    """Capture actual callbacks before lookup; never infer code from a version label.

    Managed admission compares supported resident code/literals with one source
    read. The existing worker loader must still verify that captured digest at
    execution; a later disk change refuses instead of executing under this key.
    Opaque initializers and transitive imported dependencies remain qualified.
    """
    from bioimageflow.executable_identity import runtime_callable_identity, validate_source_callables

    klass = type(tool)
    if isinstance(tool, ProcessingTool):
        names = ("process_batch",) if klass.process_batch is not ProcessingTool.process_batch else ("process_row",)
    else:
        names = ("merge_dataframes", "transform")
    callbacks = {name: getattr(tool, name) for name in names}
    canonical_module = getattr(klass, "_bif_canonical_module", klass.__module__)
    declared_version = getattr(klass, "_bif_package_version", None)
    declared_distribution = getattr(klass, "_bif_worker_distribution", None)
    if declared_version is None and not getattr(klass, "_bif_custom_source_hash", None):
        import_root = canonical_module.split(".", 1)[0]
        versions = {} if declared_versions is None else declared_versions
        if declared_distribution is not None:
            declared_version = importlib.metadata.version(declared_distribution)
        else:
            if import_root not in versions:
                try:
                    versions[import_root] = _find_distribution(import_root)
                except ValueError:
                    versions[import_root] = None
            fact = versions[import_root]
            if fact is not None:
                declared_distribution, declared_version = fact
    key: dict[str, Any] = {"module": canonical_module, "class": klass.__qualname__,
                           "declared_version": declared_version,
                           "declared_distribution": declared_distribution,
                           "declaration": _declaration_identity(tool)}
    custom_hash = getattr(klass, "_bif_custom_source_hash", None)
    origin = None
    if managed or isinstance(custom_hash, str):
        path = Path(getattr(klass, "_bif_admitted_source_file", None) or inspect.getsourcefile(klass) or inspect.getfile(klass)).resolve(strict=True)
        source = path.read_bytes()
        source_digest = hashlib.sha256(source).hexdigest()
        # Inherited callbacks have a distinct source owner; this source read does
        # not purport to validate their initializer or dependency closure.
        local_callbacks = {name: callback for name, callback in callbacks.items()
                           if Path(getattr(callback, "__func__", callback).__code__.co_filename).resolve() == path}
        qualification = validate_source_callables(source, local_callbacks, canonicalize=canonicalize)
        key.update(authority="captured_source", source_hash=custom_hash or source_digest)
        if isinstance(custom_hash, str) and getattr(klass, "_bif_admitted_source_file", None) and source_digest != custom_hash:
            raise ValueError("Admitted custom source bytes changed before executable capture")
        if managed:
            origin = resolve_worker_tool_origin(tool, _captured_source_hash=source_digest)
            if isinstance(origin, (SourceFileOriginV1, SharedModuleOriginV1)):
                # Use the SAME captured bytes, not resolve's subsequent file read.
                from dataclasses import replace
                origin = replace(origin, source_hash=source_digest)
            elif isinstance(origin, (InstalledModuleOriginV1, VersionedModuleOriginV1)):
                evidence = runtime_callable_identity(callbacks, canonicalize=canonicalize)
                key.update(authority="declared_installation", controller_digest=evidence["digest"],
                           distribution=origin.distribution, version=origin.version)
                qualification = tuple(sorted(set(qualification) | set(evidence["unresolved"]) | {
                    "installed worker/transitive byte closure unproved",
                }))
        qualification = tuple(sorted(set(qualification) | {"opaque initializer/transitive dependency closure unproved"}))
        if managed:
            qualification = tuple(sorted(set(qualification) | {
                "controller/worker class-declaration/module-initializer IO parity unproved",
            }))
    else:
        evidence = runtime_callable_identity(callbacks, canonicalize=canonicalize)
        key.update(authority="runtime_callable", runtime_digest=evidence["digest"])
        qualification = tuple(evidence["unresolved"])
    environment = getattr(tool, "environment", None)
    if environment is not None:
        # canonicalize owns a detached JSON-compatible projection, not recipe
        # objects or execution resources.
        key["declared_dependencies"] = json.loads(canonicalize(environment.dependencies))
    qualification = tuple(sorted(set(qualification) | {"custom annotation validator closure unproved"}))
    return ExecutableCapture(MappingProxyType(key), MappingProxyType(callbacks), origin, qualification)
