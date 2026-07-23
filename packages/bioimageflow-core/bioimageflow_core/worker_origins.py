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
from urllib.parse import unquote, urlparse

from bioimageflow_core.tool import ProcessingTool


ORIGIN_SCHEMA = "bioimageflow.worker_tool_origin.v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DISTRIBUTION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class InstalledModuleOriginV1:
    distribution: str
    version: str
    module: str
    class_name: str
    schema: Literal["bioimageflow.worker_tool_origin.v1"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["installed_module"] = field(default="installed_module", init=False)


@dataclass(frozen=True)
class VersionedModuleOriginV1:
    distribution: str
    import_package: str
    version: str
    canonical_module: str
    scoped_module: str
    store_root: str
    class_name: str
    schema: Literal["bioimageflow.worker_tool_origin.v1"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["versioned_module"] = field(default="versioned_module", init=False)


@dataclass(frozen=True)
class SharedModuleOriginV1:
    module: str
    import_root: str
    source_hash: str
    class_name: str
    schema: Literal["bioimageflow.worker_tool_origin.v1"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["shared_module"] = field(default="shared_module", init=False)


@dataclass(frozen=True)
class SourceFileOriginV1:
    path: str
    source_hash: str
    class_name: str
    schema: Literal["bioimageflow.worker_tool_origin.v1"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["source_file"] = field(default="source_file", init=False)


@dataclass(frozen=True)
class ArchiveModuleOriginV1:
    source_id: str
    source_hash: str
    canonical_module: str
    scoped_module: str
    materialization_root: str
    class_name: str
    schema: Literal["bioimageflow.worker_tool_origin.v1"] = field(
        default=ORIGIN_SCHEMA, init=False
    )
    kind: Literal["archive_module"] = field(default="archive_module", init=False)


WorkerToolOriginV1 = Union[
    InstalledModuleOriginV1,
    VersionedModuleOriginV1,
    SharedModuleOriginV1,
    SourceFileOriginV1,
    ArchiveModuleOriginV1,
]

_ORIGIN_TYPES: Dict[str, Tuple[Type[Any], Tuple[str, ...]]] = {
    "installed_module": (
        InstalledModuleOriginV1,
        ("distribution", "version", "module", "class_name"),
    ),
    "versioned_module": (
        VersionedModuleOriginV1,
        (
            "distribution",
            "import_package",
            "version",
            "canonical_module",
            "scoped_module",
            "store_root",
            "class_name",
        ),
    ),
    "shared_module": (
        SharedModuleOriginV1,
        ("module", "import_root", "source_hash", "class_name"),
    ),
    "source_file": (
        SourceFileOriginV1,
        ("path", "source_hash", "class_name"),
    ),
    "archive_module": (
        ArchiveModuleOriginV1,
        (
            "source_id",
            "source_hash",
            "canonical_module",
            "scoped_module",
            "materialization_root",
            "class_name",
        ),
    ),
}

_instance_lock = threading.RLock()
_instances: Dict[str, ProcessingTool] = {}


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


def encode_worker_tool_origin(origin: WorkerToolOriginV1) -> Dict[str, Any]:
    """Encode one origin to its exact plain-dictionary representation."""
    if not isinstance(
        origin, tuple(origin_type for origin_type, _ in _ORIGIN_TYPES.values())
    ):
        raise TypeError("origin must be a WorkerToolOriginV1 value.")
    payload = asdict(origin)
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


def decode_worker_tool_origin(payload: Mapping[str, Any]) -> WorkerToolOriginV1:
    """Decode an origin and reject every non-v1 or non-canonical payload."""
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


def worker_tool_origin_identity(origin: WorkerToolOriginV1) -> str:
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


def _distribution_imports(distribution: importlib.metadata.Distribution) -> set:
    declared = distribution.read_text("top_level.txt")
    if declared:
        return {
            line.strip()
            for line in declared.splitlines()
            if line.strip() and not line.startswith("#")
        }
    roots = set()
    for file in distribution.files or ():
        first = file.parts[0] if file.parts else ""
        if first and not first.endswith((".dist-info", ".data", ".pth")):
            roots.add(first.removesuffix(".py"))
    return roots


def _editable_distribution_provides(
    distribution: importlib.metadata.Distribution,
    module_name: str,
) -> bool:
    direct_url = distribution.read_text("direct_url.json")
    if not direct_url:
        return False
    parsed = json.loads(direct_url)
    url = parsed.get("url")
    if not isinstance(url, str):
        return False
    location = urlparse(url)
    if location.scheme != "file":
        return False
    project_root = Path(unquote(location.path)).resolve(strict=True)
    module_parts = module_name.split(".")
    for source_root in (project_root, project_root / "src"):
        module_path = source_root.joinpath(*module_parts)
        if (
            module_path.with_suffix(".py").is_file()
            or (module_path / "__init__.py").is_file()
        ):
            return True
    return False


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


def _load_source_file(origin: SourceFileOriginV1, identity: str) -> Any:
    path = Path(origin.path)
    if not path.is_file():
        raise ImportError(f"Worker source file does not exist: {path}.")
    if _file_hash(path) != origin.source_hash:
        raise ImportError(f"Worker source file hash mismatch: {path}.")
    module_name = f"_bioimageflow_worker_{identity}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load worker source file: {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_shared_module(origin: SharedModuleOriginV1) -> Any:
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
    if _file_hash(source_path) != origin.source_hash:
        raise ImportError(f"Shared module {origin.module!r} source hash mismatch.")
    module = _isolated_import(origin.module, origin.import_root)
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise ImportError(f"Shared module {origin.module!r} has no source file.")
    loaded_path = Path(module_file)
    if loaded_path.resolve(strict=True) != source_path.resolve(strict=True):
        raise ImportError(
            f"Shared module {origin.module!r} escaped import root {origin.import_root!r}."
        )
    return module


def _isolated_import(module_name: str, import_root: str) -> Any:
    top_package = module_name.split(".", 1)[0]
    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == top_package or name.startswith(top_package + ".")
    }
    for name in previous:
        sys.modules.pop(name, None)
    try:
        with _temporary_import_root(import_root):
            module = importlib.import_module(module_name)
        return module
    finally:
        for name in [
            candidate
            for candidate in sys.modules
            if candidate == top_package or candidate.startswith(top_package + ".")
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(previous)


def _load_versioned_module(origin: VersionedModuleOriginV1) -> Any:
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
        sys.modules[scoped_root] = package
        spec.loader.exec_module(package)
    with _temporary_import_root(origin.store_root):
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


def _load_archive_module(origin: ArchiveModuleOriginV1) -> Any:
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
    return _isolated_import(origin.scoped_module, origin.materialization_root)


def _load_origin_class(
    origin: WorkerToolOriginV1, identity: str
) -> Type[ProcessingTool]:
    if isinstance(origin, InstalledModuleOriginV1):
        _verify_distribution(origin.distribution, origin.version)
        distribution = importlib.metadata.distribution(origin.distribution)
        top_package = origin.module.split(".", 1)[0]
        if top_package not in _distribution_imports(
            distribution
        ) and not _editable_distribution_provides(distribution, origin.module):
            raise ImportError(
                f"Module {origin.module!r} is not provided by distribution "
                f"{origin.distribution!r}."
            )
        module = importlib.import_module(origin.module)
    elif isinstance(origin, VersionedModuleOriginV1):
        module = _load_versioned_module(origin)
    elif isinstance(origin, SharedModuleOriginV1):
        module = _load_shared_module(origin)
    elif isinstance(origin, SourceFileOriginV1):
        module = _load_source_file(origin, identity)
    else:
        module = _load_archive_module(origin)
    return _require_processing_tool(module, origin.class_name)


def load_worker_tool(origin: WorkerToolOriginV1) -> ProcessingTool:
    """Load and cache one tool instance by its complete canonical origin."""
    validated = decode_worker_tool_origin(encode_worker_tool_origin(origin))
    identity = worker_tool_origin_identity(validated)
    with _instance_lock:
        instance = _instances.get(identity)
        if instance is None:
            instance = _load_origin_class(validated, identity)()
            _instances[identity] = instance
        return instance


def clear_worker_tool_instances() -> None:
    """Clear the origin-aware instance cache."""
    with _instance_lock:
        _instances.clear()
