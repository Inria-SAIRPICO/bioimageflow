"""Custom-source discovery, loading, and archive helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import (
    Any,
    Iterable,
    Iterator,
    Path,
    _CustomToolBundle,
    base64,
    contextmanager,
    hashlib,
    importlib,
    inspect,
    sys,
    tempfile,
    zipfile,
)

if TYPE_CHECKING:
    from .model import Workflow


def _get_store_path() -> Path:
    from bioimageflow.tool_loader import _get_tool_store_path

    return _get_tool_store_path()


_LIBRARY_MODULE_PREFIXES = (
    "bioimageflow",
    "bioimageflow_core",
    "bioimageflow_common_tools",
    "bioimageflow_io_tools",
    "bioimageflow_measurement_tools",
    "bioimageflow_restoration_tools",
    "bioimageflow_sairpico_tools",
    "bioimageflow_segmentation_tools",
    "bioimageflow_spot_tools",
    "bioimageflow_tracking_tools",
)
_CUSTOM_TOOLS_PACKAGE = "tools"
_CUSTOM_TOOL_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache"}
_CUSTOM_TOOL_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_CUSTOM_TOOL_MAX_FILE_BYTES = 5 * 1024 * 1024


def _is_workflow_custom_class(cls: type) -> bool:
    """Return whether a class should be embedded in workflow exports."""
    if getattr(cls, "_bif_custom_source_hash", None):
        return True

    package = getattr(cls, "_bif_package", None)
    package_version = getattr(cls, "_bif_package_version", None)
    if package and package_version:
        return False

    module_name = getattr(cls, "__module__", "")
    if any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in _LIBRARY_MODULE_PREFIXES
    ):
        return False

    try:
        source_file = inspect.getsourcefile(cls) or inspect.getfile(cls)
    except (OSError, TypeError):
        return False
    path = Path(source_file)
    return path.exists() and path.suffix == ".py"


def _find_custom_tools_dir(source_file: Path) -> Path | None:
    source_file = source_file.resolve()
    for parent in source_file.parents:
        if parent.name != _CUSTOM_TOOLS_PACKAGE:
            continue
        if (parent / "__init__.py").exists():
            return parent
    return None


@contextmanager
def _workflow_import_scope(root: Path):
    """Temporarily prefer a workflow root for imports from ``tools``."""
    root_str = str(root)
    sys.path.insert(0, root_str)
    previous_tools_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == _CUSTOM_TOOLS_PACKAGE or name.startswith(_CUSTOM_TOOLS_PACKAGE + ".")
    }
    for name in list(previous_tools_modules):
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        sys.path = [entry for entry in sys.path if entry != root_str]
        for name in [
            n
            for n in sys.modules
            if n == _CUSTOM_TOOLS_PACKAGE or n.startswith(_CUSTOM_TOOLS_PACKAGE + ".")
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(previous_tools_modules)


def _extract_workflow_archive(path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            member_path = Path(member)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Invalid workflow archive member: {member!r}")
        archive.extractall(destination)


def _iter_custom_tools_files(tools_dir: Path) -> Iterator[Path]:
    for path in sorted(tools_dir.rglob("*")):
        rel_parts = path.relative_to(tools_dir).parts
        if path.is_dir():
            continue
        if any(part in _CUSTOM_TOOL_EXCLUDED_DIRS for part in rel_parts):
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.suffix in _CUSTOM_TOOL_EXCLUDED_SUFFIXES:
            continue
        if path.stat().st_size > _CUSTOM_TOOL_MAX_FILE_BYTES:
            raise ValueError(
                f"Custom workflow tool file '{path}' is too large to export "
                f"({path.stat().st_size} bytes; limit "
                f"{_CUSTOM_TOOL_MAX_FILE_BYTES} bytes). Move large assets "
                "outside tools/ and load them explicitly."
            )
        yield path


def _build_custom_tools_dir_record(
    cls: type,
    source_file: Path,
    tools_dir: Path,
) -> tuple[str, dict[str, Any]]:
    files: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in _iter_custom_tools_files(tools_dir):
        rel_path = Path(tools_dir.name) / path.relative_to(tools_dir)
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        rel_posix = rel_path.as_posix()
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
        files.append(
            {
                "path": rel_posix,
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
                "source_hash": file_hash,
            }
        )

    source_hash = digest.hexdigest()
    source_id = f"m_{source_hash[:16]}"
    return source_id, {
        "id": source_id,
        "module": cls.__module__,
        "filename": source_file.name,
        "root_package": tools_dir.name,
        "source_hash": source_hash,
        "files": files,
    }


def _get_custom_tools_dir_bundle_hash(cls: type) -> str | None:
    """Return the directory-bundle hash for a workflow-local tools/ class."""
    if not _is_workflow_custom_class(cls):
        return None
    source_file = Path(inspect.getsourcefile(cls) or inspect.getfile(cls))
    tools_dir = _find_custom_tools_dir(source_file)
    if tools_dir is None:
        return None
    _source_id, record = _build_custom_tools_dir_record(cls, source_file, tools_dir)
    return record["source_hash"]


def _workflow_custom_tools_dirs(workflow: Workflow) -> list[Path]:
    """Return workflow-local tools/ directories used by ``workflow``."""
    from bioimageflow.workflow_node import WorkflowNode

    dirs: dict[Path, None] = {}
    for node in workflow._nodes.values():
        if isinstance(node, WorkflowNode):
            for directory in _workflow_custom_tools_dirs(node.workflow):
                dirs[directory] = None
            continue

        cls = type(node.tool)
        if not _is_workflow_custom_class(cls):
            continue
        source_file = Path(inspect.getsourcefile(cls) or inspect.getfile(cls))
        tools_dir = _find_custom_tools_dir(source_file)
        if tools_dir is not None:
            dirs[tools_dir.resolve()] = None
    return list(dirs)


def _register_custom_tool_module(
    cls: type,
    *,
    records: list[dict[str, Any]],
    seen_ids: set[str],
) -> str | None:
    if not _is_workflow_custom_class(cls):
        return None

    captured_id = getattr(cls, "_bif_custom_source_id", None)
    if isinstance(captured_id, str) and captured_id in seen_ids:
        return captured_id

    source_file = Path(inspect.getsourcefile(cls) or inspect.getfile(cls))
    tools_dir = _find_custom_tools_dir(source_file)
    if tools_dir is not None:
        source_id, record = _build_custom_tools_dir_record(cls, source_file, tools_dir)
        if source_id not in seen_ids:
            records.append(record)
            seen_ids.add(source_id)
        setattr(cls, "_bif_custom_source_id", source_id)
        setattr(cls, "_bif_custom_source_hash", record["source_hash"])
        return source_id

    source = source_file.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    source_id = f"m_{source_hash[:16]}"
    if source_id not in seen_ids:
        records.append(
            {
                "id": source_id,
                "module": cls.__module__,
                "filename": source_file.name,
                "source_hash": source_hash,
                "source": source,
            }
        )
        seen_ids.add(source_id)
    setattr(cls, "_bif_custom_source_id", source_id)
    setattr(cls, "_bif_custom_source_hash", source_hash)
    return source_id


def _load_custom_sources(
    records: Iterable[dict[str, Any]],
) -> dict[str, _CustomToolBundle]:
    modules: dict[str, _CustomToolBundle] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Custom source records must be objects.")
        single_fields = {"id", "module", "filename", "source_hash", "source"}
        bundle_fields = {
            "id",
            "module",
            "filename",
            "root_package",
            "source_hash",
            "files",
        }
        expected_fields = bundle_fields if "files" in record else single_fields
        if set(record) != expected_fields:
            raise ValueError("Malformed custom source record.")
        source_id = record["id"]
        if not isinstance(source_id, str) or not source_id or source_id in modules:
            raise ValueError("Custom source IDs must be unique non-empty strings.")
        if "files" in record:
            if not isinstance(record["files"], list):
                raise ValueError("Custom source files must be an array.")
            for file_record in record["files"]:
                if not isinstance(file_record, dict) or set(file_record) != {
                    "path",
                    "encoding",
                    "content",
                    "source_hash",
                }:
                    raise ValueError("Malformed custom source file record.")
                if file_record["encoding"] != "base64":
                    raise ValueError("Custom source file encoding must be 'base64'.")
            modules[source_id] = _load_custom_tools_dir_bundle(record)
            continue

        source = record["source"]
        expected_hash = record.get("source_hash")
        actual_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise ValueError(f"Embedded custom tool module {source_id!r} hash mismatch")

        filename = record.get("filename") or f"{source_id}.py"
        module_dir = Path(tempfile.mkdtemp(prefix="bioimageflow_custom_tools_"))
        module_path = module_dir / filename
        module_path.write_text(source, encoding="utf-8")
        module_name = f"bioimageflow_custom_tools_{source_id}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load embedded custom tool module {source_id!r}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        _stamp_embedded_custom_classes(
            module,
            source_id,
            actual_hash,
            record.get("module", ""),
        )
        modules[source_id] = _CustomToolBundle(
            source_id=source_id,
            source_hash=actual_hash,
            module=module,
        )
    return modules


def _load_custom_tools_dir_bundle(record: dict[str, Any]) -> _CustomToolBundle:
    source_id = record["id"]
    expected_hash = record.get("source_hash")
    digest = hashlib.sha256()
    root_package = record.get("root_package") or _CUSTOM_TOOLS_PACKAGE
    scoped_root = f"bioimageflow_custom_tools_{source_id}"
    temp_root = Path(tempfile.mkdtemp(prefix="bioimageflow_custom_tools_"))
    package_root = temp_root / scoped_root
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")

    for file_record in record.get("files", []):
        rel_path = Path(file_record["path"])
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError(
                f"Invalid embedded custom tool path: {file_record['path']!r}"
            )
        if file_record.get("encoding") == "base64":
            data = base64.b64decode(file_record["content"])
        else:
            data = file_record["source"].encode("utf-8")
        actual_file_hash = hashlib.sha256(data).hexdigest()
        if file_record.get("source_hash") not in (None, actual_file_hash):
            raise ValueError(f"Embedded custom tool file {rel_path!s} hash mismatch")
        rel_posix = rel_path.as_posix()
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\0")
        digest.update(actual_file_hash.encode("ascii"))
        digest.update(b"\0")
        output_path = package_root / rel_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)

    actual_hash = digest.hexdigest()
    if expected_hash and expected_hash != actual_hash:
        raise ValueError(f"Embedded custom tool bundle {source_id!r} hash mismatch")
    sys.path.insert(0, str(temp_root))
    bundle = _CustomToolBundle(
        source_id=source_id,
        source_hash=actual_hash,
        scoped_root=scoped_root,
        root_package=root_package,
        sys_path=str(temp_root),
    )
    return bundle


def _resolve_custom_tool_class(
    custom_modules: dict[str, _CustomToolBundle],
    source_id: str,
    module_name: str,
    class_name: str,
) -> type:
    bundle = custom_modules[source_id]
    if bundle.module is not None:
        return getattr(bundle.module, class_name)

    assert bundle.scoped_root is not None
    assert bundle.root_package is not None
    if module_name == bundle.root_package or module_name.startswith(
        bundle.root_package + "."
    ):
        scoped_module = f"{bundle.scoped_root}.{module_name}"
    else:
        scoped_module = f"{bundle.scoped_root}.{bundle.root_package}.{module_name}"
    module = importlib.import_module(scoped_module)
    _stamp_embedded_custom_package(
        bundle.scoped_root,
        bundle.source_id,
        bundle.source_hash,
        bundle.root_package,
        bundle.sys_path,
    )
    return getattr(module, class_name)


def _stamp_embedded_custom_classes(
    module: Any,
    source_id: str,
    source_hash: str,
    canonical_module: str,
) -> None:
    from bioimageflow_core.tool import BaseTool

    for attr_name in dir(module):
        try:
            obj = getattr(module, attr_name)
        except Exception:
            continue
        if not isinstance(obj, type):
            continue
        if not issubclass(obj, BaseTool):
            continue
        if obj is BaseTool:
            continue
        if getattr(obj, "__module__", "") != getattr(module, "__name__", ""):
            continue
        setattr(obj, "_bif_custom_source_hash", source_hash)
        setattr(obj, "_bif_custom_source_id", source_id)
        if canonical_module:
            setattr(obj, "_bif_canonical_module", canonical_module)


def _stamp_embedded_custom_package(
    scoped_root: str,
    source_id: str,
    source_hash: str,
    root_package: str,
    sys_path: str | None,
) -> None:
    from bioimageflow_core.tool import BaseTool

    for module_name, module in list(sys.modules.items()):
        if module is None:
            continue
        if module_name != scoped_root and not module_name.startswith(scoped_root + "."):
            continue
        for attr_name in dir(module):
            try:
                obj = getattr(module, attr_name)
            except Exception:
                continue
            if not isinstance(obj, type):
                continue
            if not issubclass(obj, BaseTool):
                continue
            if obj is BaseTool:
                continue
            obj_module = getattr(obj, "__module__", "")
            if obj_module != scoped_root and not obj_module.startswith(
                scoped_root + "."
            ):
                continue
            canonical_module = obj.__module__
            prefix = scoped_root + "."
            if canonical_module.startswith(prefix):
                canonical_module = canonical_module[len(prefix) :]
            setattr(obj, "_bif_custom_source_hash", source_hash)
            setattr(obj, "_bif_custom_source_id", source_id)
            setattr(obj, "_bif_canonical_module", canonical_module)
            if sys_path is not None:
                setattr(obj, "_bif_worker_sys_path", sys_path)
                setattr(obj, "_bif_worker_module", obj.__module__)


def _auto_install_if_missing(pkg: str, pkg_ver: str, store: Path) -> None:
    """Install a versioned package into the tool store if missing."""
    pkg_dir = store / pkg / pkg_ver / pkg
    if pkg_dir.exists():
        return
    from bioimageflow.tool_loader import ensure_installed

    # Use the module name as the PyPI name (hyphens for underscores)
    pypi_name = pkg.replace("_", "-")
    ensure_installed(pkg, pkg_ver, pypi_name, store)
