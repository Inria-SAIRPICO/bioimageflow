"""
Unit tests for versioned tool package loading.

Covers:
- Loading a versioned package into an isolated namespace
- Two versions produce distinct classes sharing common base classes
- Relative imports resolve correctly (including nested subpackages)
- Tool classes are stamped with version metadata
- get_tool_package_info() returns correct metadata
- resolve_tool_class() finds classes in submodules
- unload removes from sys.modules; reload produces fresh objects
- Error handling for nonexistent packages/versions
- inspect.getfile() and get_source_hash() work on versioned tools
"""

import inspect
import json
import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec
from bioimageflow.dataframe_tool import DataFrameTool


# ---------------------------------------------------------------------------
# Fixture: build a tool store with two versions of dummy_tools
# ---------------------------------------------------------------------------

@pytest.fixture
def tool_store(tmp_path):
    """Create a tool store with two versions of dummy_tools.

    v1.0.0:
      - AlphaTool (ProcessingTool): Inputs(value: int = 0), Outputs(result: str)
        process_row returns result="v1"
      - LoaderTool (DataFrameTool): Inputs(path: str), Outputs(filepath: str)
      - AlphaTool uses relative import from .base (DummyBase)
      - utils/ subpackage with helpers.py (tests nested relative imports)

    v2.0.0:
      - AlphaTool (ProcessingTool): Inputs(value: int = 0, extra: int = 0),
        Outputs(result: str) — process_row returns result="v2"
      - LoaderTool (DataFrameTool): same schema but adds a "version" column = "v2"
      - Different DummyBase internals (distinct class object)
    """
    store = tmp_path / "tool_packages"

    for version, extra_field, result_value, loader_extra in [
        ("1.0.0", "", "v1", ""),
        ("2.0.0", "\n        extra: int = 0", "v2",
         "\n        df['version'] = 'v2'"),
    ]:
        pkg_dir = store / "dummy_tools" / version / "dummy_tools"
        pkg_dir.mkdir(parents=True)
        dep_dir = store / "dummy_tools" / version / "dep_pkg"
        dep_dir.mkdir()
        (dep_dir / "__init__.py").write_text("DEP_VALUE = 42\n")

        (pkg_dir / "__init__.py").write_text(
            "from .alpha import AlphaTool\n"
            "from .loader import LoaderTool\n"
        )

        (pkg_dir / "base.py").write_text(
            "from bioimageflow_core import ProcessingTool, EnvironmentSpec\n"
            "dummy_env = EnvironmentSpec(\n"
            "    name='dummy', dependencies={'pip': ['numpy']}\n"
            ")\n"
            "class DummyBase(ProcessingTool):\n"
            "    environment = dummy_env\n"
        )

        (pkg_dir / "alpha.py").write_text(
            "import dep_pkg\n"
            "from .base import DummyBase\n"
            "from .utils.helpers import helper_func\n"
            "from bioimageflow_core import IOModel, Arguments\n\n"
            "class AlphaTool(DummyBase):\n"
            f"    display_name = 'Alpha'\n"
            "    class Inputs(IOModel):\n"
            f"        value: int = 0{extra_field}\n"
            "    class Outputs(IOModel):\n"
            "        result: str\n"
            "    def process_row(self, arguments: Arguments, *, context: object | None = None):\n"
            f"        return self.Outputs(result='{result_value}')\n"
        )

        (pkg_dir / "loader.py").write_text(
            "from pathlib import Path\n"
            "import pandas as pd\n"
            "from bioimageflow.dataframe_tool import DataFrameTool\n"
            "from bioimageflow_core import IOModel\n\n"
            "class LoaderTool(DataFrameTool):\n"
            "    display_name = 'Dummy Loader'\n"
            "    class Inputs(IOModel):\n"
            "        path: str\n"
            "    class Outputs(IOModel):\n"
            "        filepath: str\n"
            "    def transform(self, df, arguments):\n"
            "        p = Path(arguments.path)\n"
            "        files = sorted(p.glob('*.tif'))\n"
            "        df = pd.DataFrame({'filepath': [str(f) for f in files]})\n"
            f"{loader_extra}\n"
            "        return df\n"
        )

        # utils/ subpackage (tests nested relative imports)
        utils_dir = pkg_dir / "utils"
        utils_dir.mkdir()
        (utils_dir / "__init__.py").write_text(
            "from .helpers import helper_func\n"
        )
        (utils_dir / "helpers.py").write_text(
            "def helper_func():\n    return 42\n"
        )

    return store


@pytest.fixture(autouse=True)
def _cleanup_sys_modules():
    """Remove any dummy_tools scoped and canonical modules after each test."""
    yield
    to_remove = [k for k in sys.modules
                 if k.startswith("dummy_tools__") or k.startswith("dummy_tools")]
    for k in to_remove:
        del sys.modules[k]
    # Clean sys.path entries that point into tmp tool stores
    sys.path[:] = [p for p in sys.path if "dummy_tools" not in p]


# ---------------------------------------------------------------------------
# Tests: load_versioned_package
# ---------------------------------------------------------------------------

class TestLoadVersionedPackage:

    def test_load_returns_module_with_tools(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        mod = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert hasattr(mod, "AlphaTool")
        assert inspect.isclass(mod.AlphaTool)

    def test_two_versions_produce_distinct_classes(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)

        assert v1.AlphaTool is not v2.AlphaTool
        # v2 has extra field; v1 does not
        v2_fields = v2.AlphaTool.Inputs._get_all_annotations()
        v1_fields = v1.AlphaTool.Inputs._get_all_annotations()
        assert "extra" in v2_fields
        assert "extra" not in v1_fields

    def test_loaded_tool_is_subclass_of_base(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert issubclass(v1.AlphaTool, ProcessingTool)
        assert issubclass(v1.LoaderTool, DataFrameTool)

    def test_relative_imports_resolve(self, tool_store):
        """AlphaTool imports from .base — loading must succeed and DummyBase
        must appear in its bases."""
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        bases = v1.AlphaTool.__bases__
        assert any(b.__name__ == "DummyBase" for b in bases)

    def test_nested_subpackage_imports(self, tool_store):
        """The utils subpackage re-exports helper_func from its __init__.py."""
        from bioimageflow.tool_loader import load_versioned_package, _scoped_name

        _v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        scoped = _scoped_name("dummy_tools", "1.0.0")
        utils_mod = sys.modules.get(f"{scoped}.utils")
        assert utils_mod is not None
        assert hasattr(utils_mod, "helper_func")
        assert utils_mod.helper_func() == 42

    def test_two_versions_have_independent_internals(self, tool_store):
        """Each version's DummyBase is a distinct class, but both are
        subclasses of ProcessingTool."""
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)

        v1_base = v1.AlphaTool.__bases__[0]
        v2_base = v2.AlphaTool.__bases__[0]
        assert v1_base is not v2_base
        assert issubclass(v1_base, ProcessingTool)
        assert issubclass(v2_base, ProcessingTool)

    def test_nonexistent_version_raises(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        with pytest.raises(FileNotFoundError):
            load_versioned_package("dummy_tools", "9.9.9", tool_store)

    def test_nonexistent_package_raises(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        with pytest.raises(FileNotFoundError):
            load_versioned_package("no_such_pkg", "1.0.0", tool_store)

    def test_cached_on_second_load(self, tool_store):
        """Second load of same version returns the cached module."""
        from bioimageflow.tool_loader import load_versioned_package

        mod1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        mod2 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert mod1 is mod2


# ---------------------------------------------------------------------------
# Tests: version metadata stamps
# ---------------------------------------------------------------------------

class TestVersionMetadata:

    def test_stamp_on_processing_tool(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert v1.AlphaTool._bif_package == "dummy_tools"
        assert v1.AlphaTool._bif_package_version == "1.0.0"
        assert v1.AlphaTool._bif_canonical_module == "dummy_tools.alpha"

    def test_stamp_on_dataframe_tool(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert v1.LoaderTool._bif_package == "dummy_tools"
        assert v1.LoaderTool._bif_package_version == "1.0.0"
        assert v1.LoaderTool._bif_canonical_module == "dummy_tools.loader"


# ---------------------------------------------------------------------------
# Tests: get_tool_package_info
# ---------------------------------------------------------------------------

class TestGetToolPackageInfo:

    def test_versioned_tool(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package, get_tool_package_info

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        pkg, ver, canonical = get_tool_package_info(v1.AlphaTool)
        assert pkg == "dummy_tools"
        assert ver == "1.0.0"
        assert canonical == "dummy_tools.alpha"

    def test_unversioned_tool(self):
        """For a locally-defined stub tool, returns (None, None, module_path)."""
        from bioimageflow.tool_loader import get_tool_package_info

        class Stub(ProcessingTool):
            display_name = "Stub"
            environment = EnvironmentSpec(name="x", dependencies={})
            class Inputs(IOModel):
                pass
            class Outputs(IOModel):
                result: str
            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result="x")

        pkg, ver, canonical = get_tool_package_info(Stub)
        assert pkg is None
        assert ver is None
        assert canonical == Stub.__module__

    def test_works_on_instance(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package, get_tool_package_info

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        instance = v1.AlphaTool()
        pkg, ver, canonical = get_tool_package_info(instance)
        assert pkg == "dummy_tools"
        assert ver == "1.0.0"


# ---------------------------------------------------------------------------
# Tests: resolve_tool_class
# ---------------------------------------------------------------------------

class TestResolveToolClass:

    def test_resolve_from_init_reexport(self, tool_store):
        """Classes re-exported in __init__.py are resolvable via the top module."""
        from bioimageflow.tool_loader import load_versioned_package, resolve_tool_class

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        cls = resolve_tool_class("dummy_tools", "1.0.0", "dummy_tools.alpha", "AlphaTool")
        assert cls._bif_package_version == "1.0.0"

    def test_resolve_from_submodule(self, tool_store):
        """Classes NOT in __init__.py are resolvable via their canonical module."""
        from bioimageflow.tool_loader import load_versioned_package, resolve_tool_class

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        # LoaderTool is in __init__.py, but test the submodule path explicitly
        cls = resolve_tool_class("dummy_tools", "1.0.0", "dummy_tools.loader", "LoaderTool")
        assert cls.__name__ == "LoaderTool"


# ---------------------------------------------------------------------------
# Tests: inspect.getfile and get_source_hash
# ---------------------------------------------------------------------------

class TestInspectIntegration:

    def test_getfile_points_to_versioned_path(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        source_file = inspect.getfile(v1.AlphaTool)
        assert "/1.0.0/dummy_tools/alpha.py" in source_file

    def test_get_source_hash_works(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.validation import get_source_hash

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        h = get_source_hash(v1.AlphaTool)
        assert h != "nosource"
        assert len(h) == 64  # SHA256 hex

    def test_source_hash_differs_across_versions(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.validation import get_source_hash

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)
        assert get_source_hash(v1.AlphaTool) != get_source_hash(v2.AlphaTool)


# ---------------------------------------------------------------------------
# Tests: unload_versioned_package
# ---------------------------------------------------------------------------

class TestUnloadVersionedPackage:

    def test_unload_removes_from_sys_modules(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package, unload_versioned_package, _scoped_name,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        scoped = _scoped_name("dummy_tools", "1.0.0")
        assert any(k.startswith(scoped) for k in sys.modules)

        unload_versioned_package("dummy_tools", "1.0.0")
        assert not any(k.startswith(scoped) for k in sys.modules)

    def test_source_hash_stable_across_reloads(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package, unload_versioned_package,
        )
        from bioimageflow.validation import get_source_hash

        mod1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        h1 = get_source_hash(mod1.AlphaTool)
        unload_versioned_package("dummy_tools", "1.0.0")

        mod2 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        h2 = get_source_hash(mod2.AlphaTool)
        assert h1 == h2

    def test_reload_after_unload_returns_fresh_module(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package, unload_versioned_package,
        )

        mod1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        cls1 = mod1.AlphaTool
        unload_versioned_package("dummy_tools", "1.0.0")

        mod2 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert mod1 is not mod2
        assert cls1 is not mod2.AlphaTool


# ---------------------------------------------------------------------------
# Tests: get_tool_version with versioned tools
# ---------------------------------------------------------------------------

class TestGetToolVersion:

    def test_returns_bif_version(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.validation import get_tool_version

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        tool = v1.AlphaTool()
        assert get_tool_version(tool) == "1.0.0"

    def test_v2_returns_different(self, tool_store):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.validation import get_tool_version

        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)
        tool = v2.AlphaTool()
        assert get_tool_version(tool) == "2.0.0"

    def test_unversioned_unchanged(self):
        """For a locally-defined stub, existing behavior applies."""
        from bioimageflow.validation import get_tool_version

        class Stub(ProcessingTool):
            display_name = "Stub Ver Test"
            environment = EnvironmentSpec(name="x", dependencies={})
            class Inputs(IOModel):
                pass
            class Outputs(IOModel):
                result: str
            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result="x")

        version = get_tool_version(Stub())
        # Should be either a package version or an mtime string, not "unversioned"
        assert version is not None
        assert isinstance(version, str)


# ---------------------------------------------------------------------------
# Tests: PEP 723 parsing
# ---------------------------------------------------------------------------

class TestParsePep723:

    def test_parse_basic_dependencies(self, tmp_path):
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "workflow.py"
        script.write_text(
            '# /// script\n'
            '# dependencies = [\n'
            '#   "simpleitk-tools==1.0.0",\n'
            '#   "cellpose-tools==2.3.1",\n'
            '# ]\n'
            '# ///\n'
            '\n'
            'print("hello")\n'
        )
        deps = _parse_pep723_dependencies(script)
        assert deps == [("simpleitk-tools", "1.0.0"), ("cellpose-tools", "2.3.1")]

    def test_parse_no_metadata_returns_empty(self, tmp_path):
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "no_meta.py"
        script.write_text('print("hello")\n')
        deps = _parse_pep723_dependencies(script)
        assert deps == []

    def test_parse_rejects_non_pinned_version(self, tmp_path):
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "flexible.py"
        script.write_text(
            '# /// script\n'
            '# dependencies = [\n'
            '#   "simpleitk-tools>=1.0",\n'
            '# ]\n'
            '# ///\n'
        )
        with pytest.raises(ValueError, match="exact.*=="):
            _parse_pep723_dependencies(script)

    def test_parse_ignores_non_tool_deps(self, tmp_path):
        """Dependencies without == pins are allowed if they aren't tool
        packages — but since we can't distinguish, we require == on all."""
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "mixed.py"
        script.write_text(
            '# /// script\n'
            '# dependencies = [\n'
            '#   "simpleitk-tools==1.0.0",\n'
            '# ]\n'
            '# requires-python = ">=3.11"\n'
            '# ///\n'
        )
        deps = _parse_pep723_dependencies(script)
        assert deps == [("simpleitk-tools", "1.0.0")]

    def test_parse_with_extra_whitespace(self, tmp_path):
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "whitespace.py"
        script.write_text(
            '# /// script\n'
            '#dependencies = [\n'
            '#  "my-tools == 3.2.0" ,\n'
            '#]\n'
            '# ///\n'
        )
        deps = _parse_pep723_dependencies(script)
        assert deps == [("my-tools", "3.2.0")]


# ---------------------------------------------------------------------------
# Tests: canonical name registration
# ---------------------------------------------------------------------------

class TestCanonicalNameRegistration:

    def test_register_enables_normal_import(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package, _register_canonical_names,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        _register_canonical_names("dummy_tools", "1.0.0")

        # Now canonical names should be in sys.modules
        assert "dummy_tools" in sys.modules
        assert "dummy_tools.alpha" in sys.modules

        # Normal import-style access should work
        mod = sys.modules["dummy_tools"]
        assert hasattr(mod, "AlphaTool")
        assert mod.AlphaTool._bif_package_version == "1.0.0"

    def test_unload_also_removes_canonical_names(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package, _register_canonical_names,
            unload_versioned_package,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        _register_canonical_names("dummy_tools", "1.0.0")
        assert "dummy_tools" in sys.modules

        unload_versioned_package("dummy_tools", "1.0.0")
        assert "dummy_tools" not in sys.modules
        assert "dummy_tools.alpha" not in sys.modules


# ---------------------------------------------------------------------------
# Tests: require_tool_packages
# ---------------------------------------------------------------------------

class TestRequireToolPackages:

    def test_require_loads_and_registers(self, tool_store, tmp_path):
        """require_tool_packages parses PEP 723, loads packages, and
        registers canonical names so normal imports work."""
        from bioimageflow.tool_loader import require_tool_packages

        script = tmp_path / "workflow.py"
        script.write_text(
            '# /// script\n'
            '# dependencies = [\n'
            '#   "dummy-tools==1.0.0",\n'
            '# ]\n'
            '# ///\n'
        )

        require_tool_packages(script, store_path=tool_store)

        # Canonical import should work
        assert "dummy_tools" in sys.modules
        mod = sys.modules["dummy_tools"]
        assert hasattr(mod, "AlphaTool")
        assert mod.AlphaTool._bif_package_version == "1.0.0"

    def test_require_empty_script(self, tmp_path, tool_store):
        """Script with no PEP 723 metadata loads nothing."""
        from bioimageflow.tool_loader import require_tool_packages

        script = tmp_path / "empty.py"
        script.write_text('print("hello")\n')

        require_tool_packages(script, store_path=tool_store)
        # Should not crash, just do nothing

    def test_require_missing_package_raises(self, tmp_path, tool_store):
        """If package isn't in the store and can't be installed, raise."""
        from bioimageflow.tool_loader import require_tool_packages

        script = tmp_path / "missing.py"
        script.write_text(
            '# /// script\n'
            '# dependencies = [\n'
            '#   "nonexistent-pkg==9.9.9",\n'
            '# ]\n'
            '# ///\n'
        )
        with pytest.raises(FileNotFoundError):
            require_tool_packages(script, store_path=tool_store, auto_install=False)


# ---------------------------------------------------------------------------
# Tests: sys.path for transitive dependencies
# ---------------------------------------------------------------------------

class TestTransitiveDeps:

    def test_store_dir_added_to_sys_path(self, tool_store):
        """The version's store directory is added to sys.path so transitive
        deps installed alongside the package are importable."""
        from bioimageflow.tool_loader import load_versioned_package

        load_versioned_package("dummy_tools", "1.0.0", tool_store)

        expected = str(tool_store / "dummy_tools" / "1.0.0")
        assert expected in sys.path

    def test_store_dir_removed_on_unload(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package, unload_versioned_package,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        expected = str(tool_store / "dummy_tools" / "1.0.0")
        assert expected in sys.path

        unload_versioned_package("dummy_tools", "1.0.0")
        assert expected not in sys.path

    @pytest.mark.compat
    def test_worker_loads_versioned_package_tool_with_relative_imports(self, tool_store):
        from bioimageflow.env_manager import _find_tool_file
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow_core.worker import _load_module_from_file

        package = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        tool_file = _find_tool_file(package.AlphaTool)
        config = json.loads(tool_file)
        assert config == {
            "mode": "versioned_module",
            "module": "dummy_tools__1_0_0.alpha",
            "package": "dummy_tools",
            "sys_path": str(tool_store / "dummy_tools" / "1.0.0"),
        }

        original_path = list(sys.path)
        for module_name in list(sys.modules):
            if module_name == "dummy_tools" or module_name.startswith("dummy_tools."):
                sys.modules.pop(module_name, None)
        sys.modules.pop("dep_pkg", None)
        try:
            sys.path[:] = [entry for entry in sys.path if entry != config["sys_path"]]
            module = _load_module_from_file(tool_file)
            alpha_tool = getattr(module, "AlphaTool")
            assert alpha_tool().process_row(None).result == "v1"
        finally:
            sys.path[:] = original_path

    def test_worker_loads_two_versioned_package_tools_without_module_collision(self, tool_store):
        from bioimageflow.env_manager import _find_tool_file
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow_core.worker import _load_module_from_file

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)

        module_v1 = _load_module_from_file(_find_tool_file(v1.AlphaTool))
        module_v2 = _load_module_from_file(_find_tool_file(v2.AlphaTool))

        assert getattr(module_v1, "AlphaTool")().process_row(None).result == "v1"
        assert getattr(module_v2, "AlphaTool")().process_row(None).result == "v2"
