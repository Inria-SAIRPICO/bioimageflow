"""Versioned tool package loading.

Loads tool packages from a versioned tool store into isolated namespaces,
allowing multiple versions of the same package to coexist in a single
orchestrator process.  Supports PEP 723 inline script metadata for
self-contained shareable workflow scripts.
"""

import importlib.machinery
import importlib.util
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from bioimageflow.paths import get_tool_store_path
from bioimageflow_core.import_context import admit_import_root, selected_import_root

from bioimageflow.filesystem import publish_no_replace
from bioimageflow.installation import admit_installation, installation_target, validate_installation_selectors

logger = logging.getLogger("bioimageflow")


def load_versioned_package(
    package: str,
    version: str,
    store_path: Path | None = None,
) -> ModuleType:
    """Load a tool package from a versioned directory into an isolated namespace.

    The package is loaded under a scoped name (e.g., ``dummy_tools__1_0_0``)
    in ``sys.modules``, so multiple versions coexist without conflict.
    Relative imports within the package resolve correctly.

    All BaseTool subclasses found in the package are stamped
    with metadata: ``_bif_package``, ``_bif_package_version``,
    ``_bif_canonical_module``.
    """
    if store_path is None:
        store_path = get_tool_store_path()
    validate_installation_selectors(package, version, None)
    target = installation_target(store_path, package, version)
    pkg_dir = target / package
    if not pkg_dir.exists():
        raise FileNotFoundError(
            f"Versioned package not found: {pkg_dir}. "
            f"Install with: bioimageflow install {package}=={version}"
        )

    admit_installation(target, package, version)
    admission = admit_import_root(target, import_package=package)

    scoped_name = _scoped_name(package, version)

    # Admit every cached namespace member before exposing a selected root.
    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == scoped_name or name.startswith(scoped_name + ".")
    }
    for name, module in previous.items():
        if module is None:
            raise ImportError(f"Cached module {name!r} has no selected store owner.")
        relative = name[len(scoped_name) :].lstrip(".").split(".")
        target = pkg_dir.joinpath(*relative) if relative != [""] else pkg_dir
        expected = (
            target / "__init__.py" if target.is_dir() else target.with_suffix(".py")
        )
        _require_selected_root(module, expected)
    if scoped_name in previous:
        cached = previous[scoped_name]
        assert cached is not None
        with selected_import_root(admission):
            return cached

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
        try:
            with selected_import_root(admission):
                assert spec.loader is not None
                spec.loader.exec_module(mod)
                _materialize_public_exports(mod)
                _stamp_tool_classes(package, version)
        except BaseException:
            owned = {
                name: module
                for name, module in sys.modules.items()
                if (name == scoped_name or name.startswith(scoped_name + "."))
                and name not in previous
            }
            for name, module in owned.items():
                if sys.modules.get(name) is module:
                    sys.modules.pop(name)
            raise
    finally:
        sys.meta_path.remove(hook)

    return mod


def _require_selected_root(module: ModuleType, expected: Path) -> None:
    source = getattr(module, "__file__", None)
    if source is None and expected.name == "__init__.py" and not expected.exists():
        locations: tuple[str, ...] = tuple(vars(module).get("__path__", ()))
        if locations and all(
            Path(location).resolve() == expected.parent.resolve()
            for location in locations
        ):
            return
    if not isinstance(source, str) or Path(source).resolve() != expected.resolve():
        raise ImportError(
            f"Cached module {module.__name__!r} conflicts with selected store root {expected.parent}."
        )


def unload_versioned_package(package: str, version: str) -> None:
    """Remove all sys.modules entries for a scoped package version.

    Also removes canonical name aliases owned by those exact module objects.
    Caller search paths are preserved.
    """
    prefix = _scoped_name(package, version)

    # Remove scoped entries and collect their module objects
    scoped_mods: set[int] = set()
    to_remove = [k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")]
    for k in to_remove:
        mod = sys.modules.pop(k, None)
        if mod is not None:
            scoped_mods.add(id(mod))

    # Remove canonical aliases (entries that point to the same module objects)
    canonical_to_remove = [
        k
        for k, mod in sys.modules.items()
        if mod is not None and id(mod) in scoped_mods
    ]
    for k in canonical_to_remove:
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
        relative = canonical_module[len(package) :]
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
    request and returns a ``ModuleSpec`` pointing at the versioned directory.
    """

    def __init__(self, scoped_prefix: str, pkg_dir: Path) -> None:
        self._prefix = scoped_prefix
        self._pkg_dir = pkg_dir

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: Any = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        if not fullname.startswith(self._prefix + "."):
            return None

        # "dummy_tools__1_0_0.alpha" -> relative = "alpha"
        relative = fullname[len(self._prefix) + 1 :]
        parts = relative.split(".")
        file_path = self._pkg_dir
        for part in parts:
            file_path = file_path / part

        # Package (directory with __init__.py)
        if file_path.is_dir() and (file_path / "__init__.py").exists():
            return importlib.util.spec_from_file_location(
                fullname,
                file_path / "__init__.py",
                submodule_search_locations=[str(file_path)],
            )

        # Regular module
        py_file = file_path.with_suffix(".py")
        if py_file.exists():
            return importlib.util.spec_from_file_location(fullname, py_file)

        return None


def _materialize_public_exports(mod: ModuleType) -> None:
    """Resolve lazy package exports declared in ``__all__``.

    Tool packages may use package-level ``__getattr__`` to lazily import
    public classes. Versioned package discovery still needs those classes
    loaded before stamping, so materialize the standard public export list
    while the scoped import hook is active.
    """
    exports = getattr(mod, "__all__", ())
    if not exports:
        return
    for name in exports:
        if not isinstance(name, str):
            continue
        getattr(mod, name)


def _stamp_tool_classes(package: str, version: str) -> None:
    """Stamp all BaseTool subclasses with version metadata."""
    from bioimageflow_core.tool import BaseTool

    scoped_prefix = _scoped_name(package, version)

    # Iterate all modules loaded under the scoped prefix
    scoped_modules = [
        mod
        for name, mod in sys.modules.items()
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
            if not issubclass(obj, BaseTool):
                continue
            if obj is BaseTool:
                continue
            # Skip classes from the orchestrator's own env (not from this package)
            obj_module = getattr(obj, "__module__", "")
            if not obj_module.startswith(scoped_prefix):
                continue

            canonical = package + obj_module[len(scoped_prefix) :]
            setattr(obj, "_bif_package", package)
            setattr(obj, "_bif_package_version", version)
            setattr(obj, "_bif_canonical_module", canonical)


# ── Canonical name registration ──────────────────────────────────────


def _register_canonical_names(package: str, version: str) -> None:
    """Register scoped modules under their canonical names in sys.modules.

    After calling this, ``from <package> import X`` works using normal
    Python imports.  This is safe when a single version of the package is
    needed (the typical PEP 723 use-case).
    """
    prefix = _scoped_name(package, version)
    aliases = {package + name[len(prefix):]: module for name, module in tuple(sys.modules.items())
               if name == prefix or name.startswith(prefix + ".")}
    for canonical, module in aliases.items():
        if canonical in sys.modules and sys.modules[canonical] is not module:
            raise ImportError(f"Canonical tool alias {canonical!r} already has a foreign owner")
    sys.modules.update(aliases)


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
        r"dependencies\s*=\s*\[(.*?)\]",
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
        pin_match = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([A-Za-z0-9_.]+)\s*$", dep)
        if not pin_match:
            raise ValueError(
                f"Tool package dependency '{dep}' must use an exact pin "
                f"(==) for reproducible workflows. "
                f'Example: "{dep.split()[0]}==1.0.0"'
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


def ensure_installed(
    pkg_name: str,
    version: str,
    pypi_name: str,
    store_path: Path,
    *,
    install_dependencies: bool = True,
) -> None:
    """Install a package into the tool store if not already present.

    Uses the current orchestrator interpreter's ``pip`` module with an
    argument-vector subprocess call. Hosts that supply compatible main-process
    dependencies can set ``install_dependencies=False`` to install only the
    tool distribution. Worker dependencies remain owned by ``EnvironmentSpec``.
    """
    validate_installation_selectors(pkg_name, version, pypi_name)
    target = installation_target(store_path, pkg_name, version)
    if target.exists() or target.is_symlink():
        admit_installation(target, pkg_name, version, distribution=pypi_name)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{version}-install-", dir=target.parent))
    logger.info("Installing %s==%s into owned staging (%s)", pypi_name, version, stage)
    primary: BaseException | None = None
    try:
        command = [sys.executable, "-m", "pip", "install", "--target", str(stage)]
        if not install_dependencies:
            command.append("--no-deps")
        command.append(f"{pypi_name}=={version}")
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            details = (exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError)
                       and exc.stderr else str(exc))
            raise RuntimeError(
                f"Failed to install {pypi_name}=={version} into tool store.\n{details}"
            ) from exc
        admit_installation(stage, pkg_name, version, distribution=pypi_name)
        publish_no_replace(stage, target)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        # stage is this invocation's exclusive namespace; never delete target.
        try:
            shutil.rmtree(stage)
        except FileNotFoundError:
            pass
        except OSError:
            if primary is None:
                raise
            logger.exception("Owned installation staging cleanup failed: %s", stage)


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

    Tool-store installation uses the current orchestrator interpreter and is
    independent of Wetlands environment configuration.

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
        automatically via the current interpreter's ``pip`` module. Set to ``False``
        to raise ``FileNotFoundError`` instead.
    """
    if store_path is None:
        store_path = _get_tool_store_path()

    deps = _parse_pep723_dependencies(script_path)

    for pypi_name, version in deps:
        pkg_name = _normalize_package_name(pypi_name)

        if auto_install:
            ensure_installed(pkg_name, version, pypi_name, store_path)

        load_versioned_package(pkg_name, version, store_path)
        _register_canonical_names(pkg_name, version)


# ── Helpers ──────────────────────────────────────────────────────────


def _get_tool_store_path() -> Path:
    """Return the tool store path, configurable via environment variable."""
    return get_tool_store_path()
