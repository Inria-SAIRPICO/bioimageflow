"""Orchestrator-side construction of strict worker tool origins."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import re
from functools import lru_cache
from dataclasses import dataclass, field
from contextlib import contextmanager, ExitStack
from bioimageflow_core.import_context import ImportRootAdmission, _host_inventory, admit_import_root, selected_import_root
from bioimageflow_core.primary_content import PrimaryContentAdmission, capture_primary_content, primary_import_context
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

from bioimageflow_core import (
    ArchiveModuleOrigin,
    InstalledModuleOrigin,
    ProcessingTool,
    SharedModuleOrigin,
    SourceFileOrigin,
    VersionedModuleOrigin,
    WorkerToolOrigin,
)

EXECUTION_CONTRACT_VERSION = "bioimageflow.execution.v2"


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


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
    import_package: str, *, root: Path | None = None,
    package_map: Mapping[str, list[str]] | None = None,
) -> tuple[str, str]:
    if root is None:
        inventory = _host_inventory()
        candidates = [inventory["facts"][id(dist)]
                      for dist in inventory["imports"].get(import_package, ())]
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


@lru_cache(maxsize=None)
def _host_distribution(import_package: str, distribution: str | None) -> tuple[str, str] | None:
    """Freeze host metadata at first selection; controller changes require restart."""
    if distribution is not None:
        canonical = _canonical_distribution(distribution)
        providers = _host_inventory()["providers"].get(canonical, ())
        if len(providers) != 1:
            raise ValueError(f"Cannot resolve exactly one host distribution for {distribution!r}")
        return _host_inventory()["facts"][id(providers[0])]
    try:
        return _find_distribution(import_package)
    except ValueError:
        return None


@dataclass
class ExecutableMetadata:
    """Metadata facts owned by one admission operation, never a global cache."""

    _imports: dict[tuple[str, str, str | None], ImportRootAdmission] = field(default_factory=dict)
    _generations: set[Any] = field(default_factory=set)
    _primary_origins: dict[type, tuple[WorkerToolOrigin, PrimaryContentAdmission]] = field(default_factory=dict)
    _source_bytes: dict[Path, bytes] = field(default_factory=dict)

    def worker_origin(self, tool: Any) -> tuple[WorkerToolOrigin, PrimaryContentAdmission]:
        klass = type(tool)
        if klass not in self._primary_origins:
            self._primary_origins[klass] = _resolve_worker_tool_origin(tool)
        elif not _class_primary_is_fixed(klass):
            # Source is captured once per operation; effective instance owners
            # still have to match that capture before actual managed lookup.
            self._primary_origins[klass][1].attest(tool)
        return self._primary_origins[klass]

    def source_bytes(self, path: Path) -> bytes:
        if path not in self._source_bytes:
            self._source_bytes[path] = path.read_bytes()
        return self._source_bytes[path]

    def generation(self, klass: type) -> Any:
        """Hold the actual loaded installation until this operation finishes."""
        from bioimageflow.tool_loader import _tool_generation
        generation = _tool_generation(klass)
        if generation is not None and generation not in self._generations:
            generation.acquire()
            self._generations.add(generation)
        return generation

    def __del__(self) -> None:
        self.clear()

    def import_admission(self, root: str, package: str, version: str | None = None) -> ImportRootAdmission:
        """Share one metadata admission per immutable selection in this operation."""
        key = (str(Path(root).resolve()), package, version)
        if key not in self._imports:
            if version is not None and _find_distribution(package, root=Path(key[0]))[1] != version:
                raise ValueError("Selected installation version changed before executable admission")
            self._imports[key] = admit_import_root(key[0], import_package=package)
        return self._imports[key]

    def resolve(self, import_root: str, distribution: str | None) -> tuple[str, str] | None:
        return _host_distribution(import_root, distribution)

    def clear(self) -> None:
        for generation in self._generations:
            generation.release()
        self._generations.clear()
        self._imports.clear()
        self._primary_origins.clear()
        self._source_bytes.clear()


@lru_cache(maxsize=None)
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


_installed_class_origins: dict[tuple[type, str], tuple[WorkerToolOrigin, PrimaryContentAdmission]] = {}
_fixed_primary_classes: set[type] = set()


def _primary_is_fixed(admission: PrimaryContentAdmission, selected: Mapping[str, bytes] | None = None) -> bool:
    """Only selected installed bytes and real host RECORD members are fixed."""
    from bioimageflow_core.import_context import _distribution_members
    inventory = _host_inventory()
    for member in admission.proof.members:
        if selected is not None and member.module in selected:
            continue
        distribution = getattr(member, "distribution", None)
        providers = inventory["providers"].get(distribution, ())
        if not any(admission.paths[member.module] in _distribution_members(provider) for provider in providers):
            return False
    return True


def _class_primary_is_fixed(klass: type) -> bool:
    from bioimageflow.tool_loader import _tool_generation
    generation = _tool_generation(klass)
    return (generation.classes.get(klass, {}).get("fixed", False) if generation is not None
            else klass in _fixed_primary_classes)


def _versioned_generation_origin(tool_class: type, generation: Any) -> tuple[WorkerToolOrigin, PrimaryContentAdmission]:
    from bioimageflow_core.import_context import _lock
    with _lock:
        facts = generation.classes.setdefault(tool_class, {})
        if "worker_origin" not in facts or not facts["fixed"]:
            admission = capture_primary_content(
                tool_class, package_root=generation.installation.root / generation.package,
                package_admission=generation.primary,
            )
            facts["fixed"] = _primary_is_fixed(admission, generation.primary.sources)
            facts["primary"] = admission
            facts["source_file"] = Path(inspect.getsourcefile(tool_class) or inspect.getfile(tool_class)).resolve(strict=True)
            facts["source_hash"] = admission.source_hash(facts["source_file"])
            facts["worker_origin"] = VersionedModuleOrigin(
                distribution=generation.installation.distribution,
                import_package=generation.package,
                version=generation.version,
                canonical_module=getattr(tool_class, "_bif_canonical_module", tool_class.__module__),
                scoped_module=tool_class.__module__,
                store_root=str(generation.installation.root),
                class_name=tool_class.__name__, primary=admission.proof,
            )
        return facts["worker_origin"], facts["primary"]


def resolve_worker_tool_origin(
    tool: ProcessingTool | type[ProcessingTool],
    *,
    installed_distribution: str | None = None,
) -> WorkerToolOrigin:
    """Construct one complete verified worker origin for a processing tool."""
    origin, _ = _resolve_worker_tool_origin(
        tool, installed_distribution=installed_distribution,
    )
    return origin


def _resolve_worker_tool_origin(
    tool: ProcessingTool | type[ProcessingTool],
    *,
    installed_distribution: str | None = None,
) -> tuple[WorkerToolOrigin, PrimaryContentAdmission]:
    from bioimageflow_core.import_context import _lock
    with _lock:
        return _resolve_worker_tool_origin_locked(tool, installed_distribution=installed_distribution)


def _resolve_worker_tool_origin_locked(
    tool: ProcessingTool | type[ProcessingTool], *, installed_distribution: str | None = None,
) -> tuple[WorkerToolOrigin, PrimaryContentAdmission]:
    """Share one held primary admission between identity and worker dispatch."""
    tool_class = tool if isinstance(tool, type) else type(tool)
    if not issubclass(tool_class, ProcessingTool):
        raise TypeError("Worker origins can only be built for ProcessingTool classes.")
    from bioimageflow.tool_loader import _generation_lease
    with _generation_lease(tool_class) as generation:
        if generation is not None:
            return _versioned_generation_origin(tool_class, generation)
    declared = installed_distribution or getattr(tool_class, "_bif_worker_distribution", None)
    if isinstance(declared, str) and (tool_class, declared) in _installed_class_origins:
        return _installed_class_origins[(tool_class, declared)]
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
        admission = capture_primary_content(tool_class, package_root=store_root / versioned_package)
        admission.attest(tool)
        return VersionedModuleOrigin(
            distribution=distribution,
            import_package=versioned_package,
            version=versioned_version,
            canonical_module=canonical_module,
            scoped_module=tool_class.__module__,
            store_root=str(store_root),
            class_name=class_name,
            primary=admission.proof,
        ), admission

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
        selected_root = Path(worker_root).resolve(strict=True) / worker_module.split(".", 1)[0]
        if not selected_root.is_dir():
            selected_root = selected_root.with_suffix(".py")
        admission = capture_primary_content(tool_class, package_root=selected_root)
        admission.attest(tool)
        return ArchiveModuleOrigin(
            source_id=source_id,
            source_hash=source_hash,
            canonical_module=canonical_module,
            scoped_module=worker_module,
            materialization_root=str(Path(worker_root).resolve(strict=True)),
            class_name=class_name,
            primary=admission.proof,
        ), admission

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
        admission = capture_primary_content(tool_class, distribution=declared_distribution)
        admission.attest(tool)
        result = InstalledModuleOrigin(
            distribution=declared_distribution,
            version=version,
            module=canonical_module,
            class_name=class_name,
            primary=admission.proof,
        ), admission
        # Editable distribution owners still capture current source on each
        # operation. Only ordinary installed RECORD members have fixed bytes.
        if _primary_is_fixed(admission):
            _installed_class_origins[(tool_class, declared_distribution)] = result
            _fixed_primary_classes.add(tool_class)
        return result

    import_root = _package_import_root(source_file, tool_class.__module__)
    if import_root is not None:
        selected_root = import_root / tool_class.__module__.split(".", 1)[0]
        if not selected_root.is_dir():
            selected_root = selected_root.with_suffix(".py")
        admission = capture_primary_content(tool_class, package_root=selected_root)
        admission.attest(tool)
        source_hash = admission.source_hash(source_file)
        return SharedModuleOrigin(
            module=tool_class.__module__,
            import_root=str(import_root),
            source_hash=source_hash,
            class_name=class_name,
            primary=admission.proof,
        ), admission
    admission = capture_primary_content(tool_class, package_root=source_file)
    admission.attest(tool)
    source_hash = admission.source_hash(source_file)
    return SourceFileOrigin(
        path=str(source_file),
        source_hash=source_hash,
        class_name=class_name,
        primary=admission.proof,
    ), admission


@dataclass(frozen=True)
class ExecutableCapture:
    """One controller admission shared by identity, provenance and dispatch."""

    scientific_key: Mapping[str, Any]
    callbacks: Mapping[str, Callable[..., Any]]
    worker_origin: WorkerToolOrigin | None
    qualification: tuple[str, ...]
    import_admission: ImportRootAdmission | None = None
    _owned_metadata: ExecutableMetadata | None = field(default=None, repr=False, compare=False)
    _generation: Any = field(default=None, repr=False, compare=False)

    @contextmanager
    def execution_context(self):
        if self._generation is not None:
            self._generation.acquire()
        try:
            with ExitStack() as contexts:
                if self.import_admission is not None:
                    contexts.enter_context(selected_import_root(self.import_admission))
                if self._generation is not None:
                    contexts.enter_context(primary_import_context(self._generation.primary))
                yield
        finally:
            if self._generation is not None:
                self._generation.release()

    def dependency_provenance(self) -> tuple[dict[str, str], ...]:
        return () if self.import_admission is None else self.import_admission.observed_dependencies


def _declaration_identity(tool: Any) -> dict[str, Any]:
    """Use the same portable projection as the actual Processing worker."""
    from bioimageflow_core.declarations import describe_io_model, describe_tool_declaration
    from bioimageflow.dataframe_tool import Passthrough

    if isinstance(tool, ProcessingTool):
        return describe_tool_declaration(tool)
    outputs = tool.Outputs
    return {
        "inputs": describe_io_model(tool.Inputs),
        "outputs": describe_io_model(outputs, passthrough=outputs is not None and issubclass(outputs, Passthrough)),
    }


def tool_diagnostic_facts(tool: Any, *, metadata: ExecutableMetadata) -> dict[str, Any]:
    """Fresh cheap pending-plan facts, never authority for cache lookup or dispatch."""
    klass = type(tool)
    generation = metadata.generation(klass)
    facts = {
        "module": getattr(klass, "_bif_canonical_module", klass.__module__),
        "class": klass.__qualname__,
        "declared_version": getattr(klass, "_bif_package_version", None),
        "declaration": _declaration_identity(tool),
        "custom_source_hash": getattr(klass, "_bif_custom_source_hash", None),
        "execution_contract": EXECUTION_CONTRACT_VERSION,
    }
    if generation is not None:
        facts.update(generation.primary.scientific_facts())
        facts.update(generation.import_admission.to_scientific_facts())
    return facts


def capture_tool_executable(
    tool: Any, *, managed: bool, canonicalize: Callable[[Any], str],
    metadata: ExecutableMetadata | None = None,
) -> ExecutableCapture:
    """Capture actual callbacks before lookup; never infer code from a version label.

    Installed primary facts stay bound to their admitted generation; editable
    owners are freshly captured once per operation. Effective declarations and
    bound callbacks remain per-node facts. Worker dispatch carries that held
    origin; opaque initializer and transitive dependency closure remain qualified.
    """
    from bioimageflow_core.executable_identity import runtime_callable_identity, validate_source_callables
    from bioimageflow_core.declarations import DECLARATION_CONTRACT_VERSION

    klass = type(tool)
    owned_metadata = ExecutableMetadata() if metadata is None else None
    admission_metadata = owned_metadata if metadata is None else metadata
    assert admission_metadata is not None
    generation = admission_metadata.generation(klass)
    if isinstance(tool, ProcessingTool):
        names = ("process_batch",) if klass.process_batch is not ProcessingTool.process_batch else ("process_row",)
    else:
        names = ("merge_dataframes", "transform")
    callbacks = {name: getattr(tool, name) for name in names}
    identity_callbacks = callbacks
    if isinstance(tool, ProcessingTool) and not managed:
        identity_callbacks = dict(callbacks)
        for name in ("process_row", "process_batch"):
            if name not in identity_callbacks:
                identity_callbacks[name] = getattr(tool, name)
    canonical_module = getattr(klass, "_bif_canonical_module", klass.__module__)
    declared_version = getattr(klass, "_bif_package_version", None)
    declared_distribution = getattr(klass, "_bif_worker_distribution", None)
    if declared_version is None and not getattr(klass, "_bif_custom_source_hash", None):
        import_root = canonical_module.split(".", 1)[0]
        fact = admission_metadata.resolve(import_root, declared_distribution)
        if fact is not None:
            declared_distribution, declared_version = fact
    key: dict[str, Any] = {"module": canonical_module, "class": klass.__qualname__,
                           "declared_version": declared_version,
                           "declared_distribution": declared_distribution,
                           "execution_contract": EXECUTION_CONTRACT_VERSION,
                           "declaration": _declaration_identity(tool),
                           "declaration_contract": DECLARATION_CONTRACT_VERSION}
    custom_hash = getattr(klass, "_bif_custom_source_hash", None)
    origin = None
    import_admission = None
    package = getattr(klass, "_bif_package", None)
    if isinstance(package, str) and declared_version is not None:
        if generation is not None:
            import_admission = generation.import_admission
        else:
            source_file = Path(inspect.getsourcefile(klass) or inspect.getfile(klass)).resolve(strict=True)
            selected_root = _versioned_store_root(source_file, package, declared_version)
            import_admission = admission_metadata.import_admission(str(selected_root), package, declared_version)
        key.update(import_admission.to_scientific_facts())
    if managed or isinstance(custom_hash, str):
        if managed:
            origin, primary_admission = admission_metadata.worker_origin(tool)
        path = (generation.classes[klass]["source_file"] if managed and generation is not None
                else Path(getattr(klass, "_bif_admitted_source_file", None) or inspect.getsourcefile(klass) or inspect.getfile(klass)).resolve(strict=True))
        if managed:
            source_digest = (generation.classes[klass]["source_hash"] if generation is not None
                             else primary_admission.source_hash(path))
            qualification = primary_admission.qualification
            key.update(primary_admission.scientific_facts())
        else:
            source = admission_metadata.source_bytes(path)
            source_digest = hashlib.sha256(source).hexdigest()
            local_callbacks = {name: callback for name, callback in identity_callbacks.items()
                               if Path(getattr(callback, "__func__", callback).__code__.co_filename).resolve() == path}
            qualification = validate_source_callables(source, local_callbacks, canonicalize=canonicalize)
            evidence = runtime_callable_identity(identity_callbacks, canonicalize=canonicalize)
            key["runtime_digest"] = evidence["digest"]
            qualification = tuple(sorted(set(qualification) | set(evidence["unresolved"])))
        key.update(authority="captured_source", source_hash=custom_hash or source_digest)
        if isinstance(custom_hash, str) and getattr(klass, "_bif_admitted_source_file", None) and source_digest != custom_hash:
            raise ValueError("Admitted custom source bytes changed before executable capture")
        if managed:
            if isinstance(origin, (InstalledModuleOrigin, VersionedModuleOrigin)):
                evidence = runtime_callable_identity(callbacks, canonicalize=canonicalize)
                key.update(authority="declared_installation", controller_digest=evidence["digest"],
                           distribution=origin.distribution, version=origin.version)
                qualification = tuple(sorted(set(qualification) | set(evidence["unresolved"]) | {
                    "installed transitive dependency closure unproved",
                }))
        qualification = tuple(sorted(set(qualification) | {"opaque initializer/transitive dependency closure unproved"}))
        if managed:
            qualification = tuple(sorted(set(qualification) | {
                "module initializer/arbitrary post-load class state closure unproved",
            }))
    else:
        evidence = runtime_callable_identity(identity_callbacks, canonicalize=canonicalize)
        key.update(authority="runtime_callable", runtime_digest=evidence["digest"])
        qualification = tuple(evidence["unresolved"])
    environment = getattr(tool, "environment", None)
    if environment is not None:
        # canonicalize owns a detached JSON-compatible projection, not recipe
        # objects or execution resources.
        key["declared_dependencies"] = json.loads(canonicalize(environment.dependencies))
    qualification = tuple(sorted(set(qualification) | {"custom annotation validator closure unproved"}))
    return ExecutableCapture(MappingProxyType(key), MappingProxyType(callbacks), origin, qualification, import_admission, owned_metadata, generation)
