"""Focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect

import json

import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec

from bioimageflow.dataframe_tool import DataFrameTool


pytest_plugins = ("tests.testkit.tool_loader",)


class TestParsePep723:
    def test_parse_basic_dependencies(self, tmp_path):
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "workflow.py"
        script.write_text(
            "# /// script\n"
            "# dependencies = [\n"
            '#   "simpleitk-tools==1.0.0",\n'
            '#   "cellpose-tools==2.3.1",\n'
            "# ]\n"
            "# ///\n"
            "\n"
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
            "# /// script\n"
            "# dependencies = [\n"
            '#   "simpleitk-tools>=1.0",\n'
            "# ]\n"
            "# ///\n"
        )
        with pytest.raises(ValueError, match="exact.*=="):
            _parse_pep723_dependencies(script)

    def test_parse_ignores_non_tool_deps(self, tmp_path):
        """Dependencies without == pins are allowed if they aren't tool
        packages — but since we can't distinguish, we require == on all."""
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "mixed.py"
        script.write_text(
            "# /// script\n"
            "# dependencies = [\n"
            '#   "simpleitk-tools==1.0.0",\n'
            "# ]\n"
            '# requires-python = ">=3.11"\n'
            "# ///\n"
        )
        deps = _parse_pep723_dependencies(script)
        assert deps == [("simpleitk-tools", "1.0.0")]

    def test_parse_with_extra_whitespace(self, tmp_path):
        from bioimageflow.tool_loader import _parse_pep723_dependencies

        script = tmp_path / "whitespace.py"
        script.write_text(
            '# /// script\n#dependencies = [\n#  "my-tools == 3.2.0" ,\n#]\n# ///\n'
        )
        deps = _parse_pep723_dependencies(script)
        assert deps == [("my-tools", "3.2.0")]


class TestCanonicalNameRegistration:
    def test_register_enables_normal_import(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package,
            _register_canonical_names,
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
            load_versioned_package,
            _register_canonical_names,
            unload_versioned_package,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        _register_canonical_names("dummy_tools", "1.0.0")
        assert "dummy_tools" in sys.modules

        unload_versioned_package("dummy_tools", "1.0.0")
        assert "dummy_tools" not in sys.modules
        assert "dummy_tools.alpha" not in sys.modules
