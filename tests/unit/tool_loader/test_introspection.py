"""Focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect

import json

import sys

import pytest

from bioimageflow_core import ProcessingTool, RowConsumption, IOModel, EnvironmentSpec

from bioimageflow.dataframe_tool import DataFrameTool


pytest_plugins = ("tests.testkit.tool_loader",)


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


class TestUnloadVersionedPackage:
    def test_unload_removes_from_sys_modules(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package,
            unload_versioned_package,
            _scoped_name,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        scoped = _scoped_name("dummy_tools", "1.0.0")
        assert any(k.startswith(scoped) for k in sys.modules)

        unload_versioned_package("dummy_tools", "1.0.0")
        assert not any(k.startswith(scoped) for k in sys.modules)

    def test_source_hash_stable_across_reloads(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package,
            unload_versioned_package,
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
            load_versioned_package,
            unload_versioned_package,
        )

        mod1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        cls1 = mod1.AlphaTool
        unload_versioned_package("dummy_tools", "1.0.0")

        mod2 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert mod1 is not mod2
        assert cls1 is not mod2.AlphaTool


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
            row_consumption = RowConsumption.MAPPED
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
