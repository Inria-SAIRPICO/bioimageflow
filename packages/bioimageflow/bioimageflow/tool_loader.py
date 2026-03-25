"""Versioned tool package loading.

Loads tool packages from a versioned tool store into isolated namespaces,
allowing multiple versions of the same package to coexist in a single
orchestrator process.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_TOOL_STORE = Path.home() / ".bioimageflow" / "tool_packages"


def load_versioned_package(
    package: str,
    version: str,
    store_path: Path = DEFAULT_TOOL_STORE,
) -> ModuleType:
    """Load a tool package from a versioned directory into an isolated namespace.

    The package is loaded under a scoped name (e.g., ``dummy_tools__1_0_0``)
    in ``sys.modules``, so multiple versions coexist without conflict.
    Relative imports within the package resolve correctly.

    All BaseTool and SubWorkflow subclasses found in the package are stamped
    with metadata: ``_bif_package``, ``_bif_package_version``,
    ``_bif_canonical_module``.
    """
    pkg_dir = store_path / package / version / package
    if not pkg_dir.exists():
        raise FileNotFoundError(
            f"Versioned package not found: {pkg_dir}. "
            f"Install with: bioimageflow install {package}=={version}"
        )

    scoped_name = _scoped_name(package, version)

    # Return cached if already loaded
    if scoped_name in sys.modules:
        return sys.modules[scoped_name]

    # Register top-level package
    init_path = pkg_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        scoped_name,
        init_path,
        submodule_search_locations=[str(pkg_dir)],
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = scoped_name
    sys.modules[scoped_name] = mod

    # Install an import hook so that `from .alpha import X` resolves
    # submodules under the scoped name using the versioned directory
    hook = _ScopedImporter(scoped_name, pkg_dir)
    sys.meta_path.insert(0, hook)
    try:
        assert spec.loader is not None
        spec.loader.exec_module(mod)
    finally:
        sys.meta_path.remove(hook)

    # Stamp tool classes with version metadata
    _stamp_tool_classes(package, version)

    return mod


def unload_versioned_package(package: str, version: str) -> None:
    """Remove all sys.modules entries for a scoped package version."""
    prefix = _scoped_name(package, version)
    to_remove = [
        k for k in sys.modules
        if k == prefix or k.startswith(f"{prefix}.")
    ]
    for k in to_remove:
        del sys.modules[k]


def get_tool_package_info(tool: Any) -> tuple[str | None, str | None, str]:
    """Return (package, version, canonical_module) for a tool class or instance."""
    cls = tool if isinstance(tool, type) else type(tool)
    package = getattr(cls, "_bif_package", None)
    version = getattr(cls, "_bif_package_version", None)
    canonical = getattr(cls, "_bif_canonical_module", cls.__module__)
    return package, version, canonical


def resolve_tool_class(
    package: str,
    version: str,
    canonical_module: str,
    class_name: str,
) -> type:
    """Resolve a tool class from a loaded versioned package.

    Given the canonical module path (e.g., ``dummy_tools.alpha``) and the
    class name, find the class in the corresponding scoped module.
    """
    scoped = _scoped_name(package, version)

    # Convert canonical module to scoped: "dummy_tools.alpha" -> "dummy_tools__1_0_0.alpha"
    if canonical_module.startswith(package):
        relative = canonical_module[len(package):]
        scoped_module = scoped + relative
    else:
        scoped_module = scoped

    if scoped_module in sys.modules:
        return getattr(sys.modules[scoped_module], class_name)

    # Fallback: try the top-level module (class re-exported in __init__)
    if scoped in sys.modules:
        mod = sys.modules[scoped]
        if hasattr(mod, class_name):
            return getattr(mod, class_name)

    raise ImportError(
        f"Cannot resolve {class_name} from {canonical_module} "
        f"(scoped: {scoped_module}). Package may not be loaded."
    )


def _scoped_name(package: str, version: str) -> str:
    """Convert package + version into a scoped module name."""
    return f"{package}__{version.replace('.', '_')}"


class _ScopedImporter:
    """Meta-path finder that resolves submodule imports under a scoped namespace.

    When code inside a versioned package does ``from .alpha import X``,
    Python looks for ``<scoped_name>.alpha``. This finder intercepts that
    request and loads the module from the versioned directory.
    """

    def __init__(self, scoped_prefix: str, pkg_dir: Path) -> None:
        self._prefix = scoped_prefix
        self._pkg_dir = pkg_dir

    def find_module(self, fullname: str, path: Any = None) -> Any:
        if fullname.startswith(self._prefix + "."):
            return self
        return None

    def load_module(self, fullname: str) -> ModuleType:
        if fullname in sys.modules:
            return sys.modules[fullname]

        # "dummy_tools__1_0_0.alpha" -> relative = "alpha"
        relative = fullname[len(self._prefix) + 1:]
        parts = relative.split(".")
        file_path = self._pkg_dir
        for part in parts:
            file_path = file_path / part

        # Check if it's a package (directory with __init__.py)
        if file_path.is_dir() and (file_path / "__init__.py").exists():
            init_file = file_path / "__init__.py"
            spec = importlib.util.spec_from_file_location(
                fullname,
                init_file,
                submodule_search_locations=[str(file_path)],
            )
            assert spec is not None
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = fullname
            sys.modules[fullname] = mod
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod

        # Otherwise it's a regular module
        py_file = file_path.with_suffix(".py")
        if py_file.exists():
            spec = importlib.util.spec_from_file_location(fullname, py_file)
            assert spec is not None
            mod = importlib.util.module_from_spec(spec)
            # Set __package__ to the parent package
            parent = fullname.rsplit(".", 1)[0] if "." in fullname else ""
            mod.__package__ = parent
            sys.modules[fullname] = mod
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod

        raise ImportError(f"No module named '{fullname}' (looked in {file_path})")


def _stamp_tool_classes(package: str, version: str) -> None:
    """Stamp all BaseTool and SubWorkflow subclasses with version metadata."""
    from bioimageflow_core.tool import BaseTool
    from bioimageflow.sub_workflow import SubWorkflow

    scoped_prefix = _scoped_name(package, version)
    base_classes = (BaseTool, SubWorkflow)

    # Iterate all modules loaded under the scoped prefix
    scoped_modules = [
        mod for name, mod in sys.modules.items()
        if (name == scoped_prefix or name.startswith(f"{scoped_prefix}."))
        and mod is not None
    ]

    for module in scoped_modules:
        for attr_name in dir(module):
            try:
                obj = getattr(module, attr_name)
            except Exception:
                continue
            if not isinstance(obj, type):
                continue
            if not issubclass(obj, base_classes):
                continue
            if obj in base_classes:
                continue
            # Skip classes from the orchestrator's own env (not from this package)
            obj_module = getattr(obj, "__module__", "")
            if not obj_module.startswith(scoped_prefix):
                continue

            obj._bif_package = package
            obj._bif_package_version = version
            canonical = package + obj_module[len(scoped_prefix):]
            obj._bif_canonical_module = canonical
