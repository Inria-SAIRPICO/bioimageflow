"""Focused methods extracted from the workflow façade."""

# Pyright checks the complete contract on Workflow; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false

from __future__ import annotations

from typing import TYPE_CHECKING, overload

from .common import (
    Any,
    Callable,
    Literal,
    Path,
    ProgressEvent,
    ValidationError,
    _path_is_within,
    cast,
    copy,
    get_active_workflow,
    importlib,
    json,
    sys,
    tempfile,
    uuid,
)
from .custom_sources import (
    _extract_workflow_archive,
    _load_custom_sources,
    _workflow_import_scope,
)

if TYPE_CHECKING:
    from .model import Workflow


class _LoadingMixin:
    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        storage_path: str | Path,
    ) -> "Workflow":
        """Deserialize a JSON file or portable archive for runtime execution."""
        path = Path(path)
        if path.suffix == ".zip":
            return cls._load_archive(path, storage_path=storage_path)
        data = json.loads(path.read_text())
        result = cls.from_dict(data, storage_path=storage_path)
        assert isinstance(result, cls)  # strict mode
        return result

    @classmethod
    def from_python(
        cls,
        path_or_module: str | Path | Any,
        *,
        storage_path: str | Path,
    ) -> "Workflow":
        """Call a trusted module's ``build_workflow(storage_path=...)`` once."""
        import types

        cleanup_callback: Callable[[], None] | None
        if isinstance(path_or_module, types.ModuleType):
            module = path_or_module
            cleanup_callback = None
        else:
            candidate = (
                Path(path_or_module)
                if isinstance(path_or_module, (str, Path))
                else None
            )
            if (
                candidate is not None
                and candidate.suffix == ".py"
                and candidate.exists()
            ):
                source_path = candidate.resolve()
                source_root = source_path.parent
                captured = {
                    path.relative_to(source_root): path.read_bytes()
                    for path in source_root.rglob("*.py")
                    if "__pycache__" not in path.parts
                }
                temp_root = Path(
                    tempfile.mkdtemp(prefix="bioimageflow_python_definition_")
                )
                for relative, content in captured.items():
                    destination = temp_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                entry = temp_root / source_path.relative_to(source_root)
                module_name = f"_bioimageflow_definition_{uuid.uuid4().hex}"
                spec = importlib.util.spec_from_file_location(module_name, entry)
                if spec is None or spec.loader is None:
                    raise ImportError(
                        f"Cannot load workflow definition from '{source_path}'."
                    )
                module = importlib.util.module_from_spec(spec)
                old_path = list(sys.path)
                previous_local = {
                    name: loaded
                    for name, loaded in sys.modules.items()
                    if getattr(loaded, "__file__", None)
                    and _path_is_within(Path(cast(str, loaded.__file__)), source_root)
                }
                for name in previous_local:
                    sys.modules.pop(name, None)
                sys.modules[module_name] = module
                sys.path.insert(0, str(temp_root))

                def cleanup_file_import() -> None:
                    sys.path[:] = old_path
                    for name, loaded in list(sys.modules.items()):
                        loaded_file = getattr(loaded, "__file__", None)
                        if loaded_file and _path_is_within(
                            Path(loaded_file), temp_root
                        ):
                            sys.modules.pop(name, None)
                    sys.modules.update(previous_local)

                cleanup_callback = cleanup_file_import

                try:
                    spec.loader.exec_module(module)
                except Exception:
                    cleanup_callback()
                    raise
            elif isinstance(path_or_module, str):
                module = importlib.import_module(path_or_module)
                cleanup_callback = None
            else:
                raise TypeError(
                    "from_python expects a module, import name, or Python file path."
                )

        try:
            factory = getattr(module, "build_workflow")
            if not callable(factory):
                raise TypeError("build_workflow must be callable.")
            workflow = factory(storage_path=storage_path)
            if not isinstance(workflow, cls):
                raise TypeError(
                    "build_workflow must return a Workflow and nothing else."
                )
            if get_active_workflow() is workflow:
                raise RuntimeError(
                    "build_workflow returned with its Workflow still active."
                )
            captured_export = workflow.to_dict(include_custom_tools=True)
            if "custom_sources" in captured_export:
                workflow._captured_custom_sources = copy.deepcopy(
                    captured_export["custom_sources"]
                )
            return workflow
        finally:
            if cleanup_callback is not None:
                cleanup_callback()

    @classmethod
    def _load_archive(
        cls,
        path: Path,
        *,
        storage_path: str | Path,
    ) -> "Workflow":
        temp_root = Path(tempfile.mkdtemp(prefix="bioimageflow_workflow_archive_"))
        _extract_workflow_archive(path, temp_root)
        workflow_path = temp_root / "workflow.json"
        if not workflow_path.exists():
            raise ValueError("Workflow archive is missing workflow.json")
        with _workflow_import_scope(temp_root):
            data = json.loads(workflow_path.read_text(encoding="utf-8"))
            result = cls.from_dict(data, storage_path=storage_path)
            assert isinstance(result, cls)  # strict mode
            return result

    @classmethod
    def import_archive(
        cls,
        path: str | Path,
        destination: str | Path,
        *,
        storage_path: str | Path,
    ) -> "Workflow":
        """Extract a portable archive and load it with explicit runtime storage."""
        path = Path(path)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        _extract_workflow_archive(path, destination)
        workflow_path = destination / "workflow.json"
        if not workflow_path.exists():
            raise ValueError("Workflow archive is missing workflow.json")
        with _workflow_import_scope(destination):
            data = json.loads(workflow_path.read_text(encoding="utf-8"))
            result = cls.from_dict(data, storage_path=storage_path)
            assert isinstance(result, cls)  # strict mode
            return result

    @overload
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        storage_path: str | Path,
        validate_only: Literal[True],
        partial: bool = False,
        auto_install: bool = True,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "tuple[Workflow, list[ValidationError]]": ...

    @overload
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        storage_path: str | Path,
        validate_only: Literal[False] = False,
        partial: bool = False,
        auto_install: bool = True,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "Workflow": ...

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        storage_path: str | Path,
        validate_only: bool = False,
        partial: bool = False,
        auto_install: bool = True,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        engine: str | None = None,
        execution: str | None = None,
        wetlands_config: dict[str, Any] | None = None,
    ) -> "Workflow | tuple[Workflow, list[ValidationError]]":
        """Reconstruct a Workflow from a serialized dict.

        Parameters
        ----------
        data
            A schema-version-1 recursive graph produced by :meth:`to_dict`,
            or a portable archive envelope produced by :meth:`export`.
        validate_only
            Drives the **return type**. When ``True``, returns a
            ``(workflow, errors)`` tuple; the workflow may be partial.
            When ``False`` (default), returns the ``Workflow`` directly
            and aggregates any captured errors into a raised exception.
        partial
            Drives **error suppression / continuation**. When ``True``,
            per-node failures are captured as :class:`ValidationError`
            entries and construction continues; the workflow may be
            best-effort partially wired. When ``False`` (default),
            construction stops at the first failure.
        auto_install
            When True (default), missing versioned packages are installed
            automatically. When False, missing packages produce an
            ``unknown_tool`` error (when captured) or raise.
        storage_path
            Runtime storage root for cache records, provenance, run views,
            transient workspaces, and materialized outputs.
        on_progress, engine, execution, wetlands_config
            Passed to :class:`Workflow`. ``None`` means "use the values
            from ``data['config']`` (or defaults)".

        Notes
        -----
        The ``partial=False, validate_only=True`` combination returns a
        ``(workflow, errors)`` tuple where ``errors`` contains at most
        one entry (the first failure) and the workflow may be empty —
        useful as a fail-fast diagnostic.
        """
        errors: list[ValidationError] = []
        try:
            wf = cls._from_recursive_dict(
                data,
                auto_install=auto_install,
                storage_path=storage_path,
                on_progress=on_progress,
                engine=engine,
                execution=execution,
                wetlands_config=wetlands_config,
                partial=partial,
                errors=errors,
            )
        except Exception as exc:
            if not validate_only:
                if partial and errors:
                    raise ValueError(
                        f"Workflow construction failed with {len(errors)} error(s)."
                    ) from exc
                raise
            errors.append(ValidationError(kind="construction_failed", message=str(exc)))
            wf = cls(
                storage_path=storage_path,
                engine=engine or "wetlands",
                execution=execution or "parallel",
            )
        wf._build_errors = list(errors)
        if validate_only:
            return wf, errors
        if errors:
            raise ValueError(
                f"Workflow construction failed with {len(errors)} error(s)."
            )
        return wf

    @classmethod
    def _from_recursive_dict(
        cls,
        data: dict[str, Any],
        *,
        auto_install: bool,
        storage_path: str | Path,
        on_progress: Callable[[ProgressEvent], None] | None,
        engine: str | None,
        execution: str | None,
        wetlands_config: dict[str, Any] | None,
        partial: bool = False,
        errors: list[ValidationError] | None = None,
    ) -> "Workflow":
        """Materialize a strict graph or portable archive envelope."""
        if set(data) == {"archive_version", "workflow", "custom_sources"}:
            if data["archive_version"] != 1 or not isinstance(
                data["custom_sources"], list
            ):
                raise ValueError("Unsupported workflow archive envelope.")
            graph = data["workflow"]
            source_records = data["custom_sources"]
        else:
            graph = data
            source_records = []
        if not isinstance(graph, dict):
            raise TypeError("Workflow graph must be a dictionary.")
        custom_modules = _load_custom_sources(source_records)
        return cls._materialize_graph(
            graph,
            custom_modules=custom_modules,
            source_records=source_records,
            auto_install=auto_install,
            storage_path=storage_path,
            on_progress=on_progress,
            engine=engine,
            execution=execution,
            wetlands_config=wetlands_config,
            partial=partial,
            errors=errors,
        )
