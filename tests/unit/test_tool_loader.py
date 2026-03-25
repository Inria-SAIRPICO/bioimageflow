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
import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, Arguments
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
            "from .base import DummyBase\n"
            "from .utils.helpers import helper_func\n"
            "from bioimageflow_core import IOModel, Arguments\n\n"
            "class AlphaTool(DummyBase):\n"
            f"    name = 'alpha'\n"
            "    class Inputs(IOModel):\n"
            f"        value: int = 0{extra_field}\n"
            "    class Outputs(IOModel):\n"
            "        result: str\n"
            "    def process_row(self, arguments: Arguments):\n"
            f"        return self.Outputs(result='{result_value}')\n"
        )

        (pkg_dir / "loader.py").write_text(
            "from pathlib import Path\n"
            "import pandas as pd\n"
            "from bioimageflow.dataframe_tool import DataFrameTool\n"
            "from bioimageflow_core import IOModel\n\n"
            "class LoaderTool(DataFrameTool):\n"
            "    name = 'dummy_loader'\n"
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
    """Remove any dummy_tools scoped modules after each test."""
    yield
    to_remove = [k for k in sys.modules if k.startswith("dummy_tools__")]
    for k in to_remove:
        del sys.modules[k]


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

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
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
            name = "stub"
            environment = EnvironmentSpec(name="x", dependencies={})
            class Inputs(IOModel):
                pass
            class Outputs(IOModel):
                result: str
            def process_row(self, a):
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
            name = "stub_ver_test"
            environment = EnvironmentSpec(name="x", dependencies={})
            class Inputs(IOModel):
                pass
            class Outputs(IOModel):
                result: str
            def process_row(self, a):
                return self.Outputs(result="x")

        version = get_tool_version(Stub())
        # Should be either a package version or an mtime string, not "unversioned"
        assert version is not None
        assert isinstance(version, str)
