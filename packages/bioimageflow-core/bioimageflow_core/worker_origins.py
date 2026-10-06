"""Strict worker-safe tool origins and origin-aware loading."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Dict, Iterator, Literal, Mapping, Optional, Tuple, Type, Union

from bioimageflow_core.import_context import _import_names, _owns, admit_import_root, selected_import_root
from bioimageflow_core.tool import ProcessingTool
from bioimageflow_core.primary_content import (
    PrimaryContentProof, admit_primary_content, decode_primary_content,
    encode_primary_content, primary_import_context, require_primary_coverage,
)


ORIGIN_SCHEMA = "bioimageflow.worker_tool_origin.v2"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DISTRIBUTION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class InstalledModuleOrigin:
    distribution: str
    version: str
    module: str
    class_name: str
    primary: PrimaryContentProof
    schema: Literal["bioimageflow.worker_tool_origin.v2"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["installed_module"] = field(default="installed_module", init=False)


@dataclass(frozen=True)
class VersionedModuleOrigin:
    distribution: str
    import_package: str
    version: str
    canonical_module: str
    scoped_module: str
    store_root: str
    class_name: str
    primary: PrimaryContentProof
    schema: Literal["bioimageflow.worker_tool_origin.v2"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["versioned_module"] = field(default="versioned_module", init=False)


@dataclass(frozen=True)
class SharedModuleOrigin:
    module: str
    import_root: str
    source_hash: str
    class_name: str
    primary: PrimaryContentProof
    schema: Literal["bioimageflow.worker_tool_origin.v2"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["shared_module"] = field(default="shared_module", init=False)


@dataclass(frozen=True)
class SourceFileOrigin:
    path: str
    source_hash: str
    class_name: str
    primary: PrimaryContentProof
    schema: Literal["bioimageflow.worker_tool_origin.v2"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["source_file"] = field(default="source_file", init=False)


@dataclass(frozen=True)
class ArchiveModuleOrigin:
    source_id: str
    source_hash: str
    canonical_module: str
    scoped_module: str
    materialization_root: str
    class_name: str
    primary: PrimaryContentProof
    schema: Literal["bioimageflow.worker_tool_origin.v2"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["archive_module"] = field(default="archive_module", init=False)


WorkerToolOrigin = Union[
    InstalledModuleOrigin,
    VersionedModuleOrigin,
    SharedModuleOrigin,
    SourceFileOrigin,
    ArchiveModuleOrigin,
]

_ORIGIN_TYPES: Dict[str, Tuple[Type[Any], Tuple[str, ...]]] = {
    "installed_module": (
        InstalledModuleOrigin,
        ("distribution", "version", "module", "class_name", "primary"),
    ),
    "versioned_module": (
        VersionedModuleOrigin,
        (
            "distribution",
            "import_package",
            "version",
            "canonical_module",
            "scoped_module",
            "store_root",
            "class_name",
            "primary",
        ),
    ),
    "shared_module": (
        SharedModuleOrigin,
        ("module", "import_root", "source_hash", "class_name", "primary"),
    ),
    "source_file": (
        SourceFileOrigin,
        ("path", "source_hash", "class_name", "primary"),
    ),
    "archive_module": (
        ArchiveModuleOrigin,
        (
            "source_id",
            "source_hash",
            "canonical_module",
            "scoped_module",
            "materialization_root",
            "class_name",
            "primary",
        ),
    ),
}

_instance_lock = threading.RLock()
_instances: Dict[str, ProcessingTool] = {}
_source_modules: Dict[str, Any] = {}


def _require_exact_keys(
    payload: Mapping[str, Any], expected: Tuple[str, ...], label: str
) -> None:
    actual = set(payload)
    wanted = {"schema", "kind", *expected}
    if actual != wanted:
        missing = sorted(wanted - actual)
        extra = sorted(actual - wanted)
        raise ValueError(
            f"{label} fields do not match the schema; missing={missing}, extra={extra}."
        )


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty normalized string.")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains invalid control characters.")
    return value


def _require_module(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if _MODULE_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a canonical Python module name.")
    return text


def _require_class(value: Any) -> str:
    text = _require_text(value, "class_name")
    if _CLASS_RE.fullmatch(text) is None:
        raise ValueError("class_name must be a canonical Python identifier.")
    return text


def _require_hash(value: Any, label: str = "source_hash") -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _require_distribution(value: Any) -> str:
    text = _require_text(value, "distribution")
    if _DISTRIBUTION_RE.fullmatch(text) is None:
        raise ValueError("distribution must use its canonical normalized spelling.")
    return text


def _require_path(value: Any, label: str) -> str:
    text = _require_text(value, label)
    path = Path(text)
    if not path.is_absolute() or os.path.normpath(text) != text:
        raise ValueError(f"{label} must be an absolute normalized path.")
    return text


def _require_safe_id(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if _SAFE_ID_RE.fullmatch(text) is None:
        raise ValueError(f"{label} contains invalid characters.")
    return text


def encode_worker_tool_origin(origin: WorkerToolOrigin) -> Dict[str, Any]:
    """Encode one origin to its exact plain-dictionary representation."""
    if not isinstance(
        origin, tuple(origin_type for origin_type, _ in _ORIGIN_TYPES.values())
    ):
        raise TypeError("origin must be a WorkerToolOrigin value.")
    payload = asdict(origin)
    payload["primary"] = encode_primary_content(origin.primary)
    return {
        key: payload[key]
        for key in (
            "schema",
            "kind",
            *(
                field_name
                for field_name in payload
                if field_name not in {"schema", "kind"}
            ),
        )
    }


def decode_worker_tool_origin(payload: Mapping[str, Any]) -> WorkerToolOrigin:
    """Decode an origin and reject every non-current or non-canonical payload."""
    if type(payload) is not dict:
        raise ValueError("Worker tool origin must be a plain object.")
    if payload.get("schema") != ORIGIN_SCHEMA:
        raise ValueError(
            f"Unsupported worker tool origin schema: {payload.get('schema')!r}."
        )
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _ORIGIN_TYPES:
        raise ValueError(f"Unsupported worker tool origin kind: {kind!r}.")
    origin_type, fields = _ORIGIN_TYPES[kind]
    _require_exact_keys(payload, fields, f"{kind} origin")

    values = {name: payload[name] for name in fields}
    values["primary"] = decode_primary_content(values["primary"])
    if not values["primary"].callbacks:
        raise ValueError("Worker origin requires effective primary callback owners.")
    if "distribution" in values:
        values["distribution"] = _require_distribution(values["distribution"])
    if "version" in values:
        values["version"] = _require_text(values["version"], "version")
    for name in ("module", "import_package", "canonical_module", "scoped_module"):
        if name in values:
            values[name] = _require_module(values[name], name)
    if "class_name" in values:
        values["class_name"] = _require_class(values["class_name"])
    if "source_hash" in values:
        values["source_hash"] = _require_hash(values["source_hash"])
    if "source_id" in values:
        values["source_id"] = _require_safe_id(values["source_id"], "source_id")
    for name in ("path", "store_root", "import_root", "materialization_root"):
        if name in values:
            values[name] = _require_path(values[name], name)

    if kind == "versioned_module":
        import_package = values["import_package"]
        canonical_module = values["canonical_module"]
        if canonical_module != import_package and not canonical_module.startswith(
            import_package + "."
        ):
            raise ValueError("canonical_module must be inside import_package.")
        relative = canonical_module[len(import_package) :]
        if relative and not values["scoped_module"].endswith(relative):
            raise ValueError(
                "scoped_module must preserve the canonical module's relative path."
            )

    return origin_type(**values)


def worker_tool_origin_identity(origin: WorkerToolOrigin) -> str:
    """Return the canonical complete-origin SHA-256 instance identity."""
    validated = decode_worker_tool_origin(encode_worker_tool_origin(origin))
    canonical = json.dumps(
        encode_worker_tool_origin(validated),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _distribution_version(distribution: str, path: Optional[str] = None) -> str:
    if path is None:
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ImportError(
                f"Distribution {distribution!r} is not installed."
            ) from exc

    matches = [
        candidate
        for candidate in importlib.metadata.distributions(path=[path])
        if _canonical_distribution(candidate.metadata["Name"]) == distribution
    ]
    if len(matches) != 1:
        raise ImportError(
            f"Expected exactly one {distribution!r} distribution under {path!r}."
        )
    return matches[0].version


def _verify_distribution(
    distribution: str, version: str, path: Optional[str] = None
) -> None:
    actual = _distribution_version(distribution, path)
    if actual != version:
        raise ImportError(
            f"Distribution {distribution!r} version mismatch: expected {version!r}, "
            f"found {actual!r}."
        )


def _require_processing_tool(module: Any, class_name: str) -> Type[ProcessingTool]:
    try:
        candidate = getattr(module, class_name)
    except AttributeError as exc:
        raise ImportError(
            f"Processing tool class {class_name!r} was not found in {module.__name__!r}."
        ) from exc
    if not isinstance(candidate, type) or not issubclass(candidate, ProcessingTool):
        raise TypeError(
            f"{module.__name__}.{class_name} is not a ProcessingTool class."
        )
    return candidate


def _module_source(module: Any) -> Path:
    source = getattr(module, "__file__", None)
    if not isinstance(source, str):
        raise ImportError(
            f"Module {getattr(module, '__name__', None)!r} has no source file."
        )
    return Path(source).resolve(strict=True)


def _require_class_root(candidate: Type[ProcessingTool], root: Path) -> None:
    defining = sys.modules.get(candidate.__module__)
    if defining is None or not _path_within(_module_source(defining), root):
        raise ImportError(
            f"Tool defining module {candidate.__module__!r} is outside selected root {root}."
        )


@contextmanager
def _temporary_import_root(root: str) -> Iterator[None]:
    sys.path.insert(0, root)
    try:
        yield
    finally:
        try:
            sys.path.remove(root)
        except ValueError:
            pass


def _load_source_file(origin: SourceFileOrigin, identity: str, primary: Any) -> Any:
    path = Path(origin.path)
    if not path.is_file():
        raise ImportError(f"Worker source file does not exist: {path}.")
    selected = next((name for name, member_path in primary.paths.items() if member_path == path.resolve(strict=True)), None)
    if selected is None:
        raise ImportError("Selected source file has no primary content member.")
    contents = primary.sources[selected]
    if hashlib.sha256(contents).hexdigest() != origin.source_hash:
        raise ImportError(f"Worker source file hash mismatch: {path}.")
    module_name = f"_bioimageflow_worker_{identity}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load worker source file: {path}.")
    previous = sys.modules.get(module_name)
    if previous is not None and _module_source(previous) != path.resolve(strict=True):
        raise ImportError(f"Cached source module conflicts with selected file {path}.")
    if previous is not None:
        if _source_modules.get(module_name) is not previous:
            raise ImportError(
                f"Cached source module {module_name!r} was not admitted by this loader."
            )
        return previous
    module = importlib.util.module_from_spec(spec)
    primary.own_module(module)
    sys.modules[module_name] = module
    try:
        exec(compile(contents, str(path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        if sys.modules.get(module_name) is module:
            if previous is None:
                sys.modules.pop(module_name)
            else:
                sys.modules[module_name] = previous
        raise
    _source_modules[module_name] = module
    return module


def _load_shared_module(origin: SharedModuleOrigin, primary: Any) -> Any:
    root = Path(origin.import_root)
    if not root.is_dir():
        raise ImportError(f"Shared import root does not exist: {root}.")
    module_path = root.joinpath(*origin.module.split("."))
    source_path = (
        module_path / "__init__.py"
        if module_path.is_dir()
        else module_path.with_suffix(".py")
    )
    if not source_path.is_file() or not _path_within(source_path, root):
        raise ImportError(
            f"Shared module {origin.module!r} is absent from {origin.import_root!r}."
        )
    if primary.source_hash(source_path) != origin.source_hash:
        raise ImportError(f"Shared module {origin.module!r} source hash mismatch.")
    module = _selected_import(origin.module, origin.import_root, origin.class_name)
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise ImportError(f"Shared module {origin.module!r} has no source file.")
    loaded_path = Path(module_file)
    if loaded_path.resolve(strict=True) != source_path.resolve(strict=True):
        raise ImportError(
            f"Shared module {origin.module!r} escaped import root {origin.import_root!r}."
        )
    return module


def _selected_import(module_name: str, import_root: str, class_name: str) -> Any:
    """Import without withdrawing or substituting a canonical namespace owner."""
    top_package = module_name.split(".", 1)[0]
    with _temporary_import_root(import_root):
        module = importlib.import_module(module_name)
        package_root = Path(import_root) / top_package
        if not package_root.is_dir():
            package_root = package_root.with_suffix(".py")
        _require_class_root(_require_processing_tool(module, class_name), package_root)
    return module


def _load_versioned_module(origin: VersionedModuleOrigin, primary: Any) -> Any:
    root = Path(origin.store_root)
    if not root.is_dir():
        raise ImportError(f"Versioned store root does not exist: {root}.")
    _verify_distribution(origin.distribution, origin.version, origin.store_root)
    package_dir = root / origin.import_package
    init_path = package_dir / "__init__.py"
    if not init_path.is_file() or not _path_within(init_path, root):
        raise ImportError(
            f"Versioned import package {origin.import_package!r} is absent from "
            f"{origin.store_root!r}."
        )

    relative = origin.canonical_module[len(origin.import_package) :]
    relative_parts = tuple(part for part in relative.split(".") if part)
    target = package_dir.joinpath(*relative_parts)
    target_source = (
        target / "__init__.py" if target.is_dir() else target.with_suffix(".py")
    )
    if not target_source.is_file() or not _path_within(target_source, root):
        raise ImportError(
            f"Versioned module {origin.canonical_module!r} is absent from "
            f"{origin.store_root!r}."
        )
    scoped_root = (
        origin.scoped_module[: -len(relative)] if relative else origin.scoped_module
    )
    cached = sys.modules.get(scoped_root)
    if cached is not None and _module_source(cached) != init_path.resolve(strict=True):
        raise ImportError(
            f"Versioned package {scoped_root!r} conflicts with selected store root {root}."
        )
    before = {
        name: module
        for name, module in sys.modules.items()
        if name == scoped_root or name.startswith(scoped_root + ".")
    }
    try:
        return _import_versioned(
            origin, init_path, package_dir, target_source, scoped_root, primary
        )
    except BaseException:
        for name, module in list(sys.modules.items()):
            if (
                name == scoped_root or name.startswith(scoped_root + ".")
            ) and name not in before and primary.owned_modules.get(name) is module:
                if sys.modules.get(name) is module:
                    sys.modules.pop(name)
        raise


def _import_versioned(
    origin: VersionedModuleOrigin,
    init_path: Path,
    package_dir: Path,
    target_source: Path,
    scoped_root: str,
    primary: Any,
) -> Any:
    if scoped_root not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            scoped_root,
            init_path,
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load versioned package from {init_path}.")
        package = importlib.util.module_from_spec(spec)
        package.__package__ = scoped_root
        primary.own_module(package)
        sys.modules[scoped_root] = package
        selected = next((name for name, path in primary.paths.items() if path == init_path.resolve(strict=True)), None)
        if selected is None:
            raise ImportError("Versioned initializer has no primary content member.")
        exec(compile(primary.sources[selected], str(init_path), "exec", dont_inherit=True), vars(package))
    module = importlib.import_module(origin.scoped_module)
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str) or Path(module_file).resolve(
        strict=True
    ) != target_source.resolve(strict=True):
        raise ImportError(
            f"Versioned module {origin.scoped_module!r} escaped store root "
            f"{origin.store_root!r}."
        )
    return module


def _archive_tree_hash(package_root: Path) -> str:
    digest = hashlib.sha256()
    entries = list(package_root.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise ImportError(f"Archive source contains a symlink: {path}.")
        if not path.is_file() and not path.is_dir():
            raise ImportError(f"Archive source contains a special file: {path}.")
    files = [
        path
        for path in entries
        if path.is_file()
        and "__pycache__" not in path.parts
        and not (
            path.parent == package_root
            and path.name == "__init__.py"
            and path.stat().st_size == 0
        )
    ]
    for path in sorted(
        files, key=lambda item: item.relative_to(package_root).as_posix()
    ):
        relative = path.relative_to(package_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_hash(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _load_archive_module(origin: ArchiveModuleOrigin) -> Any:
    root = Path(origin.materialization_root)
    if not root.is_dir():
        raise ImportError(f"Archive materialization root does not exist: {root}.")
    top_package = origin.scoped_module.split(".", 1)[0]
    package_root = root / top_package
    module_path = root.joinpath(*origin.scoped_module.split("."))
    source_path = (
        module_path / "__init__.py"
        if module_path.is_dir()
        else module_path.with_suffix(".py")
    )
    if not source_path.is_file() or not _path_within(source_path, root):
        raise ImportError(
            f"Archive module {origin.scoped_module!r} is absent from "
            f"{origin.materialization_root!r}."
        )
    actual_hash = (
        _archive_tree_hash(package_root)
        if package_root.is_dir()
        else _file_hash(source_path)
    )
    if actual_hash != origin.source_hash:
        raise ImportError(f"Archive source {origin.source_id!r} hash mismatch.")
    return _selected_import(
        origin.scoped_module, origin.materialization_root, origin.class_name
    )


def _load_origin_class(
    origin: WorkerToolOrigin, identity: str, primary: Any
) -> Type[ProcessingTool]:
    with _instance_lock:
        distribution: Optional[importlib.metadata.Distribution] = None
        if isinstance(origin, InstalledModuleOrigin):
            _verify_distribution(origin.distribution, origin.version)
            distribution = importlib.metadata.distribution(origin.distribution)
            top_package = origin.module.split(".", 1)[0]
            if top_package not in _import_names(distribution):
                raise ImportError(
                    f"Module {origin.module!r} is not provided by distribution "
                    f"{origin.distribution!r}."
                )
            module = importlib.import_module(origin.module)
        elif isinstance(origin, VersionedModuleOrigin):
            module = _load_versioned_module(origin, primary)
        elif isinstance(origin, SharedModuleOrigin):
            module = _load_shared_module(origin, primary)
        elif isinstance(origin, SourceFileOrigin):
            module = _load_source_file(origin, identity, primary)
        else:
            module = _load_archive_module(origin)
        candidate = _require_processing_tool(module, origin.class_name)
        if isinstance(origin, InstalledModuleOrigin):
            assert distribution is not None
            defining = sys.modules.get(candidate.__module__)
            if (
                not _owns(distribution, module.__name__, module, {}, {})
                or defining is None
                or not _owns(distribution, defining.__name__, defining, {}, {})
            ):
                raise ImportError(
                    f"Selected module or tool defining module is outside distribution {origin.distribution!r}."
                )
        elif isinstance(origin, VersionedModuleOrigin):
            _require_class_root(
                candidate, Path(origin.store_root) / origin.import_package
            )
        elif isinstance(origin, SourceFileOrigin):
            defining = sys.modules.get(candidate.__module__)
            if defining is None or _module_source(defining) != Path(
                origin.path
            ).resolve(strict=True):
                raise ImportError(
                    f"Tool defining module {candidate.__module__!r} differs from selected source file."
                )
        return candidate


def load_worker_tool(
    origin: WorkerToolOrigin, *, dependency_authority: str = "selected_installation",
) -> ProcessingTool:
    """Load one instance with explicit selected-installation or worker authority."""
    if isinstance(origin, VersionedModuleOrigin):
        admission = admit_import_root(origin.store_root, import_package=origin.import_package,
                                      dependency_authority=dependency_authority)
        with selected_import_root(admission):
            return _load_worker_tool(origin, admission=admission)
    return _load_worker_tool(origin)


def _load_worker_tool(origin: WorkerToolOrigin, *, admission: Any = None) -> ProcessingTool:
    with _selected_worker_tool(origin, admission=admission) as tool:
        return tool


@contextmanager
def _selected_worker_tool(origin: WorkerToolOrigin, *, admission: Any = None) -> Iterator[ProcessingTool]:
    """Reattest selected bytes and resident methods on every task admission."""
    validated = decode_worker_tool_origin(encode_worker_tool_origin(origin))
    identity = worker_tool_origin_identity(validated)
    primary = admit_primary_content(validated.primary)
    _require_origin_coverage(validated, primary)
    aliases = {}
    if isinstance(validated, SourceFileOrigin):
        selected = Path(validated.path).resolve(strict=True)
        aliases = {name: f"_bioimageflow_worker_{identity}"
                   for name, path in primary.paths.items() if path == selected}
        _require_owned_source_namespace(identity)
    try:
        with primary_import_context(primary, aliases=aliases):
            with _instance_lock:
                instance = _instances.get(identity)
                if instance is None:
                    candidate = _load_origin_class(validated, identity, primary)
                    primary.attest(candidate)
                    instance = candidate()
                    primary.attest(instance)
                    if admission is not None:
                        _ = admission.observed_dependencies
                    _instances[identity] = instance
                else:
                    primary.attest(instance)
            yield instance
    except BaseException:
        with _instance_lock:
            cached = _instances.get(identity)
            if cached is not None and type(cached).__module__ in primary.owned_modules:
                _instances.pop(identity)
            for name, module in primary.owned_modules.items():
                if sys.modules.get(name) is not module and _source_modules.get(name) is module:
                    _source_modules.pop(name)
        raise


def clear_worker_tool_instances() -> None:
    """Clear the origin-aware instance cache."""
    with _instance_lock:
        _instances.clear()


def _require_owned_source_namespace(identity: str) -> None:
    name = f"_bioimageflow_worker_{identity}"
    if name in sys.modules and (sys.modules[name] is None or _source_modules.get(name) is not sys.modules[name]):
        raise ImportError(f"Cached source module {name!r} was not admitted by this loader.")


def _admit_origin_class(origin: WorkerToolOrigin) -> Type[ProcessingTool]:
    """Admit a preflight class without constructing a tool instance."""
    validated = decode_worker_tool_origin(encode_worker_tool_origin(origin))
    identity = worker_tool_origin_identity(validated)
    primary = admit_primary_content(validated.primary)
    _require_origin_coverage(validated, primary)
    aliases = {}
    if isinstance(validated, SourceFileOrigin):
        selected = Path(validated.path).resolve(strict=True)
        aliases = {name: f"_bioimageflow_worker_{identity}"
                   for name, path in primary.paths.items() if path == selected}
        _require_owned_source_namespace(identity)
    with primary_import_context(primary, aliases=aliases):
        with _instance_lock:
            candidate = _load_origin_class(validated, identity, primary)
            primary.attest(candidate)
            return candidate


def _require_origin_coverage(origin: WorkerToolOrigin, primary: Any) -> None:
    if isinstance(origin, SourceFileOrigin):
        require_primary_coverage(primary, source_path=origin.path)
    elif isinstance(origin, InstalledModuleOrigin):
        _verify_distribution(origin.distribution, origin.version)
        require_primary_coverage(primary, module=origin.module,
                                 distribution=origin.distribution, version=origin.version)
    else:
        if isinstance(origin, VersionedModuleOrigin):
            module = origin.scoped_module
            root = Path(origin.store_root) / origin.import_package
        else:
            module = origin.module if isinstance(origin, SharedModuleOrigin) else origin.scoped_module
            import_root = origin.import_root if isinstance(origin, SharedModuleOrigin) else origin.materialization_root
            root = Path(import_root) / module.split(".")[0]
            if not root.is_dir():
                root = root.with_suffix(".py")
        require_primary_coverage(primary, module=module, package_root=root)
