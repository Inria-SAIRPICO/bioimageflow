"""Tests for :class:`bioimageflow.ToolRegistry`
(plan-platform-boundary-refactor.md Task 7).

The registry separates install (slow, network-bound) from register
(fast, in-process). These tests reuse the ``tool_store`` fixture from
``test_tool_loader`` to build a fake versioned package without
actually installing anything from PyPI.
"""

import sys

import pytest

from bioimageflow import ToolRegistry, ToolMetadata


@pytest.fixture
def tool_store(tmp_path):
    """Build a minimal versioned package store. Mirrors the fixture in
    ``test_tool_loader`` but trimmed to what the registry tests need.
    """
    store = tmp_path / "tool_packages"
    pkg_dir = store / "dummy_tools" / "1.0.0" / "dummy_tools"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "from .alpha import AlphaTool\n"
    )
    (pkg_dir / "alpha.py").write_text(
        "from bioimageflow_core import ProcessingTool, IOModel, Arguments\n"
        "class AlphaTool(ProcessingTool):\n"
        "    display_name = 'Alpha'\n"
        "    tags = ['demo']\n"
        "    class Inputs(IOModel):\n"
        "        value: int = 0\n"
        "    class Outputs(IOModel):\n"
        "        result: str\n"
        "    def process_row(self, arguments: Arguments):\n"
        "        return self.Outputs(result='ok')\n"
    )
    return store


@pytest.fixture(autouse=True)
def _cleanup_sys_modules():
    yield
    for k in [k for k in sys.modules if k.startswith("dummy_tools")]:
        del sys.modules[k]
    sys.path[:] = [p for p in sys.path if "dummy_tools" not in p]


class TestRegisterPackage:
    def test_register_after_manual_install(self, tool_store) -> None:
        reg = ToolRegistry(store_path=tool_store)
        metas = reg.register_package("dummy_tools", "1.0.0")
        names = {m.class_name for m in metas}
        assert "AlphaTool" in names

        cls = reg.get_class("AlphaTool")
        assert cls is not None
        assert cls.__name__ == "AlphaTool"

        meta = reg.get_metadata("AlphaTool")
        assert isinstance(meta, ToolMetadata)
        assert meta.package == "dummy_tools"
        assert meta.version == "1.0.0"
        assert meta.display_name == "Alpha"
        assert meta.tags == ("demo",)
        # Schema serializers populate inputs/outputs.
        assert "value" in meta.inputs_schema
        assert "result" in meta.outputs_schema

    def test_register_does_not_install(self, tmp_path) -> None:
        # Empty store; register must raise rather than reach for the network.
        empty_store = tmp_path / "empty"
        reg = ToolRegistry(store_path=empty_store)
        with pytest.raises(FileNotFoundError):
            reg.register_package("nope", "1.0.0")

    def test_list_tools_returns_all_registered(self, tool_store) -> None:
        reg = ToolRegistry(store_path=tool_store)
        reg.register_package("dummy_tools", "1.0.0")
        tools = reg.list_tools()
        assert any(t.class_name == "AlphaTool" for t in tools)


class TestForget:
    def test_forget_removes_class_and_metadata(self, tool_store) -> None:
        reg = ToolRegistry(store_path=tool_store)
        reg.register_package("dummy_tools", "1.0.0")
        assert reg.get_class("AlphaTool") is not None
        reg.forget("AlphaTool")
        assert reg.get_class("AlphaTool") is None
        assert reg.get_metadata("AlphaTool") is None

    def test_forget_unknown_is_noop(self, tool_store) -> None:
        reg = ToolRegistry(store_path=tool_store)
        reg.forget("does_not_exist")  # should not raise


class TestInstallPackageSeparation:
    def test_install_and_register_are_separate_methods(self) -> None:
        # Without invoking install_package (which would hit pixi), the
        # registry's API still works for already-on-disk packages —
        # this is the contract that gives GUIs a fast register path.
        reg = ToolRegistry()
        # Just smoke-test the interface — not running an actual install.
        assert hasattr(reg, "install_package")
        assert hasattr(reg, "register_package")
