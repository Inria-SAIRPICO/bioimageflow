"""Focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect

import json

import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec

from bioimageflow.dataframe_tool import DataFrameTool


pytest_plugins = ("tests.testkit.tool_loader",)


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

    def test_lazy_all_exports_are_materialized(self, lazy_tool_store):
        from bioimageflow.tool_loader import load_versioned_package, _scoped_name

        mod = load_versioned_package("lazy_tools", "1.0.0", lazy_tool_store)
        scoped = _scoped_name("lazy_tools", "1.0.0")

        assert "LazyAlpha" in vars(mod)
        assert "LazyLoader" in vars(mod)
        assert f"{scoped}.alpha" in sys.modules
        assert f"{scoped}.loader" in sys.modules
        assert issubclass(mod.LazyAlpha, ProcessingTool)
        assert issubclass(mod.LazyLoader, DataFrameTool)

    def test_lazy_all_exports_keep_versions_isolated(self, lazy_tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        v1 = load_versioned_package("lazy_tools", "1.0.0", lazy_tool_store)
        v2 = load_versioned_package("lazy_tools", "2.0.0", lazy_tool_store)

        assert v1.LazyAlpha is not v2.LazyAlpha
        assert v1.LazyAlpha._bif_package_version == "1.0.0"
        assert v2.LazyAlpha._bif_package_version == "2.0.0"

    def test_lazy_export_failure_cleans_partial_package(self, broken_lazy_tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        with pytest.raises(RuntimeError, match="cannot load BrokenTool"):
            load_versioned_package("broken_lazy_tools", "1.0.0", broken_lazy_tool_store)

        assert "broken_lazy_tools__1_0_0" not in sys.modules
        assert not any(p.endswith("broken_lazy_tools/1.0.0") for p in sys.path)


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

    def test_stamp_on_lazy_exported_tools(self, lazy_tool_store):
        from bioimageflow.tool_loader import load_versioned_package

        mod = load_versioned_package("lazy_tools", "1.0.0", lazy_tool_store)

        assert mod.LazyAlpha._bif_package == "lazy_tools"
        assert mod.LazyAlpha._bif_package_version == "1.0.0"
        assert mod.LazyAlpha._bif_canonical_module == "lazy_tools.alpha"
        assert mod.LazyLoader._bif_package == "lazy_tools"
        assert mod.LazyLoader._bif_package_version == "1.0.0"
        assert mod.LazyLoader._bif_canonical_module == "lazy_tools.loader"


class TestGetToolPackageInfo:
    def test_versioned_tool(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package,
            get_tool_package_info,
        )

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
        from bioimageflow.tool_loader import (
            load_versioned_package,
            get_tool_package_info,
        )

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        instance = v1.AlphaTool()
        pkg, ver, canonical = get_tool_package_info(instance)
        assert pkg == "dummy_tools"
        assert ver == "1.0.0"


class TestResolveToolClass:
    def test_resolve_from_init_reexport(self, tool_store):
        """Classes re-exported in __init__.py are resolvable via the top module."""
        from bioimageflow.tool_loader import load_versioned_package, resolve_tool_class

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        cls = resolve_tool_class(
            "dummy_tools", "1.0.0", "dummy_tools.alpha", "AlphaTool"
        )
        assert cls._bif_package_version == "1.0.0"

    def test_resolve_from_submodule(self, tool_store):
        """Classes NOT in __init__.py are resolvable via their canonical module."""
        from bioimageflow.tool_loader import load_versioned_package, resolve_tool_class

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        # LoaderTool is in __init__.py, but test the submodule path explicitly
        cls = resolve_tool_class(
            "dummy_tools", "1.0.0", "dummy_tools.loader", "LoaderTool"
        )
        assert cls.__name__ == "LoaderTool"
