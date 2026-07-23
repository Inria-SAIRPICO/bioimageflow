"""Focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect

import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec

from bioimageflow.dataframe_tool import DataFrameTool


pytest_plugins = ("tests.testkit.tool_loader",)


class TestRequireToolPackages:
    def test_require_loads_and_registers(self, tool_store, tmp_path):
        """require_tool_packages parses PEP 723, loads packages, and
        registers canonical names so normal imports work."""
        from bioimageflow.tool_loader import require_tool_packages

        script = tmp_path / "workflow.py"
        script.write_text(
            '# /// script\n# dependencies = [\n#   "dummy-tools==1.0.0",\n# ]\n# ///\n'
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
            "# /// script\n"
            "# dependencies = [\n"
            '#   "nonexistent-pkg==9.9.9",\n'
            "# ]\n"
            "# ///\n"
        )
        with pytest.raises(FileNotFoundError):
            require_tool_packages(script, store_path=tool_store, auto_install=False)


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
            load_versioned_package,
            unload_versioned_package,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        expected = str(tool_store / "dummy_tools" / "1.0.0")
        assert expected in sys.path

        unload_versioned_package("dummy_tools", "1.0.0")
        assert expected not in sys.path

    @pytest.mark.compat
    def test_worker_loads_versioned_package_tool_with_relative_imports(
        self, tool_store
    ):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.worker_origins import resolve_worker_tool_origin
        from bioimageflow_core import VersionedModuleOriginV1
        from bioimageflow_core.worker_origins import load_worker_tool

        package = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        origin = resolve_worker_tool_origin(package.AlphaTool)
        assert isinstance(origin, VersionedModuleOriginV1)
        assert origin.distribution == "dummy-tools"
        assert origin.import_package == "dummy_tools"
        assert origin.canonical_module == "dummy_tools.alpha"
        assert origin.scoped_module == "dummy_tools__1_0_0.alpha"
        assert origin.store_root == str(
            (tool_store / "dummy_tools" / "1.0.0").resolve()
        )

        original_path = list(sys.path)
        for module_name in list(sys.modules):
            if module_name == "dummy_tools" or module_name.startswith("dummy_tools."):
                sys.modules.pop(module_name, None)
        sys.modules.pop("dep_pkg", None)
        try:
            sys.path[:] = [
                entry for entry in sys.path if entry != origin.store_root
            ]
            assert load_worker_tool(origin).process_row(None).result == "v1"
        finally:
            sys.path[:] = original_path

    def test_worker_loads_two_versioned_package_tools_without_module_collision(
        self, tool_store
    ):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.worker_origins import resolve_worker_tool_origin
        from bioimageflow_core.worker_origins import load_worker_tool

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)

        tool_v1 = load_worker_tool(resolve_worker_tool_origin(v1.AlphaTool))
        tool_v2 = load_worker_tool(resolve_worker_tool_origin(v2.AlphaTool))

        assert tool_v1.process_row(None).result == "v1"
        assert tool_v2.process_row(None).result == "v2"
