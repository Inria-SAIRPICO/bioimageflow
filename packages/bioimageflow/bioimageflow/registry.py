"""Public tool-registry abstraction.

A :class:`ToolRegistry` wraps :func:`load_versioned_package`,
:func:`resolve_tool_class`, and :func:`serialize_input_schema` /
:func:`serialize_output_schema` behind a single object that GUIs and
other consumers can use without rebuilding metadata serialization or
package-resolution layers themselves.

The registry deliberately separates **install** (a slow, network-bound
side effect) from **register** (a fast, in-process index lookup):

- :meth:`ToolRegistry.install_package` installs a package into the
  tool store, doing the network work.
- :meth:`ToolRegistry.register_package` only loads what is already
  installed and indexes its tools — never installs.

GUIs that validate on every keystroke must call ``register_package``
on hot paths and ``install_package`` only from a user-initiated
action.

Workflow exports may also carry embedded custom tool modules. GUIs can
call :meth:`ToolRegistry.register_workflow` to discover those local tools
for the specific workflow being edited without promoting them to a
versioned tool package.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bioimageflow.paths import get_tool_store_path
from bioimageflow.tool_loader import (
    ensure_installed,
    load_versioned_package,
)
from bioimageflow.validation import (
    serialize_input_schema,
    serialize_output_schema,
)

logger = logging.getLogger("bioimageflow")


@dataclass(frozen=True)
class ToolMetadata:
    """Public, serialized description of a tool class.

    Produced by :meth:`ToolRegistry.register_package` for every
    BaseTool / SubWorkflow subclass found in the loaded package.

    Attributes
    ----------
    package
        The tool's package name (e.g. ``"my_tools"``).
    version
        Pinned package version (e.g. ``"1.2.3"``).
    module
        Canonical module path (e.g. ``"my_tools.alpha"``).
    class_name
        The class name as written in the source.
    inputs_schema
        Output of :func:`serialize_input_schema` for this class.
    outputs_schema
        Output of :func:`serialize_output_schema` for this class.
    display_name
        Human-readable label declared on the class, or the class name.
    tags
        Free-form tags declared on the class (empty list if none).
    """
    package: str
    version: str
    module: str
    class_name: str
    inputs_schema: dict[str, Any]
    outputs_schema: dict[str, Any]
    display_name: str
    tags: tuple[str, ...] = field(default_factory=tuple)


class ToolRegistry:
    """Stateful index of tool classes loaded from packages or workflows."""

    def __init__(self, *, store_path: Path | None = None) -> None:
        self._store_path: Path = (
            store_path if store_path is not None else get_tool_store_path()
        )
        self._classes: dict[str, type] = {}
        self._metadata: dict[str, ToolMetadata] = {}

    # -- install vs register ------------------------------------------------

    def install_package(self, name: str, version: str) -> None:
        """Install a versioned package into the tool store.

        This is the slow, network-bound side effect — it does not load
        or index anything. Call :meth:`register_package` separately
        once the install completes.

        ``name`` is the import name (the module a workflow expects to
        import). ``ensure_installed`` infers the PyPI name from this
        by replacing underscores with hyphens, matching the convention
        used by :meth:`Workflow.from_dict`'s auto-installer.
        """
        pypi_name = name.replace("_", "-")
        ensure_installed(name, version, pypi_name, self._store_path)

    def register_package(self, name: str, version: str) -> list[ToolMetadata]:
        """Load an *already installed* package and index its tools.

        Returns the metadata for every BaseTool / SubWorkflow subclass
        discovered in the package. Raises :class:`FileNotFoundError`
        if the package is not present in the store — the caller must
        install it first via :meth:`install_package` (or skip
        registration on the hot path).
        """
        from bioimageflow_core.tool import BaseTool
        from bioimageflow.sub_workflow import SubWorkflow

        # load_versioned_package raises FileNotFoundError if the package
        # is not installed — we deliberately propagate that error rather
        # than auto-installing.
        mod = load_versioned_package(name, version, self._store_path)

        discovered: list[ToolMetadata] = []
        seen: set[type] = set()
        unstamped: dict[type, str] = {}

        # Walk the loaded module and any submodules registered under its
        # scoped name. Tool classes stamped by _stamp_tool_classes carry
        # _bif_canonical_module pointing at the original module path.
        import sys
        scoped_prefix = mod.__name__
        for sys_name, sys_mod in list(sys.modules.items()):
            if sys_mod is None:
                continue
            if sys_name != scoped_prefix and not sys_name.startswith(
                scoped_prefix + "."
            ):
                continue
            for attr_name in dir(sys_mod):
                try:
                    obj = getattr(sys_mod, attr_name)
                except Exception:
                    continue
                if not isinstance(obj, type):
                    continue
                if obj in seen:
                    continue
                if not issubclass(obj, (BaseTool, SubWorkflow)):
                    continue
                if obj is BaseTool or obj is SubWorkflow:
                    continue
                if getattr(obj, "_bif_package", None) != name:
                    # A tool-like class that wasn't stamped for this
                    # package. If its __module__ points at the canonical
                    # package name, it's almost certainly an absolute
                    # import in __init__.py bypassing the scoped loader.
                    # Track it so we can warn once per offending class
                    # below — silent skip would leave the GUI's tool
                    # list empty with no actionable diagnostic.
                    obj_module = getattr(obj, "__module__", "")
                    if (
                        obj_module == name
                        or obj_module.startswith(name + ".")
                    ) and obj not in unstamped:
                        unstamped[obj] = obj_module
                    continue
                seen.add(obj)

                meta = self._build_metadata(obj)
                self._classes[meta.class_name] = obj
                self._metadata[meta.class_name] = meta
                discovered.append(meta)

        for cls, obj_module in unstamped.items():
            logger.warning(
                "Tool class %s.%s found in package %r v%s but missing "
                "scope marker — this usually means the package's "
                "__init__.py uses absolute imports "
                "(`from %s.module import X`) instead of relative ones "
                "(`from .module import X`). The class will not be "
                "registered. See specs.md §Tool Packages.",
                obj_module, cls.__name__, name, version, name,
            )

        return discovered

    def register_workflow(self, workflow: Any) -> list[ToolMetadata]:
        """Index custom tools carried by a workflow.

        ``workflow`` may be either a live :class:`bioimageflow.Workflow`
        instance or an exported workflow dict. Live workflows are
        inspected directly; exported dicts must contain the
        ``custom_tool_modules`` bundle written by ``Workflow.export``.
        Package tools referenced by the workflow are not loaded or
        installed here; use :meth:`register_package` for those.
        """
        from bioimageflow.sub_workflow import SubWorkflowNode
        from bioimageflow.workflow import (
            Workflow,
            _is_workflow_custom_class,
            _load_custom_tool_modules,
        )

        classes: list[type] = []
        if isinstance(workflow, Workflow):
            for node in workflow.nodes.values():
                if isinstance(node, SubWorkflowNode):
                    cls = type(node.sub_workflow)
                else:
                    cls = type(node.tool)
                if _is_workflow_custom_class(cls):
                    classes.append(cls)
        elif isinstance(workflow, dict):
            modules = _load_custom_tool_modules(
                workflow.get("custom_tool_modules", [])
            )
            for node_data in workflow.get("nodes", []):
                if node_data.get("type") == "sub_workflow":
                    source_id = node_data.get("sub_workflow_source_module")
                    class_name = node_data.get("sub_workflow_class")
                else:
                    source_id = node_data.get("tool_source_module")
                    class_name = node_data.get("tool_class")
                if not source_id or not class_name:
                    continue
                module = modules[source_id]
                classes.append(getattr(module, class_name))
        else:
            raise TypeError(
                "register_workflow expects a Workflow instance or workflow dict"
            )

        discovered: list[ToolMetadata] = []
        seen: set[type] = set()
        for cls in classes:
            if cls in seen:
                continue
            seen.add(cls)
            meta = self._build_metadata(cls)
            self._classes[meta.class_name] = cls
            self._metadata[meta.class_name] = meta
            discovered.append(meta)
        return discovered

    def _build_metadata(self, cls: type) -> ToolMetadata:
        canonical = getattr(cls, "_bif_canonical_module", cls.__module__)
        package = getattr(cls, "_bif_package", "")
        version = getattr(cls, "_bif_package_version", "")
        try:
            inputs_schema = serialize_input_schema(cls)
        except Exception:
            inputs_schema = {}
        try:
            outputs_schema = serialize_output_schema(cls)
        except Exception:
            outputs_schema = {}
        display_name = (
            getattr(cls, "display_name", None) or cls.__name__
        )
        tags = tuple(getattr(cls, "tags", ()) or ())
        return ToolMetadata(
            package=package,
            version=version,
            module=canonical,
            class_name=cls.__name__,
            inputs_schema=inputs_schema,
            outputs_schema=outputs_schema,
            display_name=display_name,
            tags=tags,
        )

    # -- lookups ------------------------------------------------------------

    def get_class(self, class_name: str) -> type | None:
        """Return the registered class, or ``None`` if not registered."""
        return self._classes.get(class_name)

    def get_metadata(self, class_name: str) -> ToolMetadata | None:
        """Return the registered :class:`ToolMetadata`, or ``None``."""
        return self._metadata.get(class_name)

    def list_tools(self) -> list[ToolMetadata]:
        """Return all registered tool metadata, in insertion order."""
        return list(self._metadata.values())

    def forget(self, class_name: str) -> None:
        """Drop a class from the registry. No-op if it is not present."""
        self._classes.pop(class_name, None)
        self._metadata.pop(class_name, None)
