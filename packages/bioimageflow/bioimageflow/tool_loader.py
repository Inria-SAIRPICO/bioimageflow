"""Versioned tool package loading.

Loads tool packages from a versioned tool store into isolated namespaces,
allowing multiple versions of the same package to coexist in a single
orchestrator process.  Supports PEP 723 inline script metadata for
self-contained shareable workflow scripts.
"""

import importlib.util
import logging
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_TOOL_STORE = Path.home() / ".bioimageflow" / "tool_packages"

logger = logging.getLogger("bioimageflow")


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

    # Add the version directory to sys.path so transitive dependencies
    # installed alongside the package (via uv pip install --target) are
    # importable by main-process code (DataFrameTools, __init__.py, etc.)
    version_dir = str(store_path / package / version)
    if version_dir not in sys.path:
        sys.path.insert(0, version_dir)

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
    """Remove all sys.modules entries for a scoped package version.

    Also removes canonical name aliases and the sys.path entry for
    transitive dependencies.
    """
    prefix = _scoped_name(package, version)

    # Remove scoped entries and collect their module objects
    scoped_mods: set[int] = set()
    to_remove = [
        k for k in sys.modules
        if k == prefix or k.startswith(f"{prefix}.")
    ]
    for k in to_remove:
        mod = sys.modules.pop(k, None)
        if mod is not None:
            scoped_mods.add(id(mod))

    # Remove canonical aliases (entries that point to the same module objects)
    canonical_to_remove = [
        k for k, mod in sys.modules.items()
        if mod is not None and id(mod) in scoped_mods
    ]
    for k in canonical_to_remove:
        del sys.modules[k]

    # Remove sys.path entries for transitive deps
    # Match any path ending with <package>/<version>
    suffix = str(Path(package) / version)
    sys.path[:] = [p for p in sys.path if not p.endswith(suffix)]


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


# ── Canonical name registration ──────────────────────────────────────

def _register_canonical_names(package: str, version: str) -> None:
    """Register scoped modules under their canonical names in sys.modules.

    After calling this, ``from <package> import X`` works using normal
    Python imports.  This is safe when a single version of the package is
    needed (the typical PEP 723 use-case).
    """
    prefix = _scoped_name(package, version)
    for scoped_key in list(sys.modules):
        if scoped_key == prefix or scoped_key.startswith(f"{prefix}."):
            # "dummy_tools__1_0_0.alpha" -> "dummy_tools.alpha"
            canonical = package + scoped_key[len(prefix):]
            sys.modules[canonical] = sys.modules[scoped_key]


# ── PEP 723 parsing ─────────────────────────────────────────────────

def _parse_pep723_dependencies(script_path: str | Path) -> list[tuple[str, str]]:
    """Extract ``(pypi_name, version)`` pairs from PEP 723 inline metadata.

    Only dependencies with exact ``==`` version pins are accepted.
    Raises ``ValueError`` for non-pinned dependencies.

    Returns an empty list if the script has no PEP 723 metadata block.
    """
    text = Path(script_path).read_text(encoding="utf-8")

    # Extract the # /// script ... # /// block
    block_re = re.compile(
        r"^# /// script\s*\n((?:#[^\n]*\n)*?)# ///\s*$",
        re.MULTILINE,
    )
    match = block_re.search(text)
    if not match:
        return []

    # Strip leading "# " from each line to get TOML content
    toml_lines = []
    for line in match.group(1).splitlines():
        stripped = line.lstrip("#").rstrip()
        # Remove at most one leading space after #
        if stripped.startswith(" "):
            stripped = stripped[1:]
        toml_lines.append(stripped)
    toml_text = "\n".join(toml_lines)

    # Parse the dependencies list from TOML
    # We use a lightweight regex approach to avoid requiring a TOML library
    deps_re = re.compile(
        r'dependencies\s*=\s*\[(.*?)\]',
        re.DOTALL,
    )
    deps_match = deps_re.search(toml_text)
    if not deps_match:
        return []

    raw_deps = deps_match.group(1)
    # Extract quoted strings
    dep_strings = re.findall(r'"([^"]+)"', raw_deps)

    result: list[tuple[str, str]] = []
    for dep in dep_strings:
        dep = dep.strip()
        # Parse "name==version" or "name == version"
        pin_match = re.match(r'^([A-Za-z0-9_.-]+)\s*==\s*([A-Za-z0-9_.]+)\s*$', dep)
        if not pin_match:
            raise ValueError(
                f"Tool package dependency '{dep}' must use an exact pin "
                f"(==) for reproducible workflows. "
                f"Example: \"{dep.split()[0]}==1.0.0\""
            )
        name = pin_match.group(1).strip()
        version = pin_match.group(2).strip()
        result.append((name, version))

    return result


def _normalize_package_name(pypi_name: str) -> str:
    """Convert a PyPI package name to a Python module name.

    ``simpleitk-tools`` → ``simpleitk_tools``
    """
    return re.sub(r"[-.]", "_", pypi_name).lower()


# ── Auto-install ─────────────────────────────────────────────────────

def _ensure_installed(
    pkg_name: str,
    version: str,
    pypi_name: str,
    store_path: Path,
) -> None:
    """Install a package into the tool store if not already present."""
    pkg_dir = store_path / pkg_name / version / pkg_name
    if pkg_dir.exists():
        return

    target = store_path / pkg_name / version
    target.mkdir(parents=True, exist_ok=True)

    logger.info("Installing %s==%s into tool store (%s)", pypi_name, version, target)
    try:
        subprocess.run(
            ["uv", "pip", "install", "--target", str(target),
             f"{pypi_name}=={version}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Could not find 'uv' on PATH. Install it with: "
            "curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    except subprocess.CalledProcessError as exc:
        # Clean up the empty directory on failure
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"Failed to install {pypi_name}=={version}:\n{exc.stderr}"
        ) from exc

    # Verify the package appeared
    if not pkg_dir.exists():
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"Installation of {pypi_name}=={version} succeeded but "
            f"expected module '{pkg_name}' not found in {target}. "
            f"Check that the PyPI package name maps to module '{pkg_name}'."
        )


# ── Top-level API ────────────────────────────────────────────────────

def require_tool_packages(
    script_path: str | Path,
    *,
    store_path: Path | None = None,
    auto_install: bool = True,
) -> None:
    """Parse PEP 723 metadata from a script, install missing packages,
    and register them under canonical names for normal imports.

    After calling this function, standard ``from <package> import Tool``
    statements work for every dependency declared in the script's
    PEP 723 ``# /// script`` block.

    Parameters
    ----------
    script_path
        Path to the Python script containing PEP 723 metadata.
        Typically ``__file__`` from the calling script.
    store_path
        Override the tool store directory.  Defaults to
        ``~/.bioimageflow/tool_packages/`` (or ``$BIOIMAGEFLOW_TOOL_STORE``).
    auto_install
        If ``True`` (default), missing packages are installed
        automatically via ``uv pip install --target``.  Set to ``False``
        to raise ``FileNotFoundError`` instead.
    """
    if store_path is None:
        store_path = _get_tool_store_path()

    deps = _parse_pep723_dependencies(script_path)

    for pypi_name, version in deps:
        pkg_name = _normalize_package_name(pypi_name)

        if auto_install:
            _ensure_installed(pkg_name, version, pypi_name, store_path)

        load_versioned_package(pkg_name, version, store_path)
        _register_canonical_names(pkg_name, version)


# ── Helpers ──────────────────────────────────────────────────────────

def _get_tool_store_path() -> Path:
    """Return the tool store path, configurable via environment variable."""
    import os
    return Path(os.environ.get(
        "BIOIMAGEFLOW_TOOL_STORE",
        str(DEFAULT_TOOL_STORE),
    ))


