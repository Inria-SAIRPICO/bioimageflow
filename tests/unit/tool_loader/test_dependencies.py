"""Focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect

import json

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

    def test_worker_loads_two_versioned_package_tools_without_module_collision(
        self, tool_store
    ):
        from bioimageflow.env_manager import _find_tool_file
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow_core.worker import _load_module_from_file

        v1 = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        v2 = load_versioned_package("dummy_tools", "2.0.0", tool_store)

        module_v1 = _load_module_from_file(_find_tool_file(v1.AlphaTool))
        module_v2 = _load_module_from_file(_find_tool_file(v2.AlphaTool))

        assert getattr(module_v1, "AlphaTool")().process_row(None).result == "v1"
        assert getattr(module_v2, "AlphaTool")().process_row(None).result == "v2"
