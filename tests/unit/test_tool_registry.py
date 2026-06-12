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
    pkg_dir_v2 = store / "dummy_tools" / "2.0.0" / "dummy_tools"
    pkg_dir_v2.mkdir(parents=True)
    (pkg_dir_v2 / "__init__.py").write_text(
        "from .alpha import AlphaTool\n"
    )
    (pkg_dir_v2 / "alpha.py").write_text(
        "from bioimageflow_core import ProcessingTool, IOModel, Arguments\n"
        "class AlphaTool(ProcessingTool):\n"
        "    display_name = 'Alpha v2'\n"
        "    tags = ['demo', 'v2']\n"
        "    class Inputs(IOModel):\n"
        "        value: int = 0\n"
        "    class Outputs(IOModel):\n"
        "        result: str\n"
        "    def process_row(self, arguments: Arguments):\n"
        "        return self.Outputs(result='ok-v2')\n"
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

    def test_register_keeps_same_class_name_from_multiple_versions(
        self, tool_store
    ) -> None:
        reg = ToolRegistry(store_path=tool_store)
        reg.register_package("dummy_tools", "1.0.0")
        reg.register_package("dummy_tools", "2.0.0")

        metas = [
            meta
            for meta in reg.list_tools()
            if meta.package == "dummy_tools" and meta.class_name == "AlphaTool"
        ]
        assert [(meta.class_name, meta.version) for meta in metas] == [
            ("AlphaTool", "1.0.0"),
            ("AlphaTool", "2.0.0"),
        ]
        alpha_v1 = reg.get_metadata(
            "AlphaTool", package="dummy_tools", version="1.0.0"
        )
        alpha_v2 = reg.get_metadata(
            "AlphaTool", package="dummy_tools", version="2.0.0"
        )
        alpha_cls = reg.get_class("AlphaTool")
        assert alpha_v1 is not None
        assert alpha_v2 is not None
        assert alpha_cls is not None
        assert alpha_v1.display_name == "Alpha"
        assert alpha_v2.display_name == "Alpha v2"
        assert alpha_cls._bif_package_version == "2.0.0"


class TestForget:
    def test_forget_removes_class_and_metadata(self, tool_store) -> None:
        reg = ToolRegistry(store_path=tool_store)
        reg.register_package("dummy_tools", "1.0.0")
        reg.register_package("dummy_tools", "2.0.0")
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


@pytest.fixture
def tool_store_absolute_imports(tmp_path):
    """A versioned package whose __init__.py uses absolute imports.

    This is a spec violation (specs.md §"Tool packages") but is the
    exact regression that left the platform's tool list empty when
    bioimageflow_common_tools shipped with absolute imports. The
    registry must warn loudly so the failure is diagnosable.
    """
    store = tmp_path / "tool_packages"
    pkg_dir = store / "abs_tools" / "1.0.0" / "abs_tools"
    pkg_dir.mkdir(parents=True)
    # Absolute import bypasses the scoped loader — tool classes are
    # loaded under the canonical name, so _stamp_tool_classes skips
    # them and they end up in the scoped namespace without _bif_package.
    (pkg_dir / "__init__.py").write_text(
        "from abs_tools.alpha import AlphaTool\n"
    )
    (pkg_dir / "alpha.py").write_text(
        "from bioimageflow_core import ProcessingTool, IOModel, Arguments\n"
        "class AlphaTool(ProcessingTool):\n"
        "    class Inputs(IOModel):\n"
        "        value: int = 0\n"
        "    class Outputs(IOModel):\n"
        "        result: str\n"
        "    def process_row(self, arguments: Arguments):\n"
        "        return self.Outputs(result='ok')\n"
    )
    yield store
    for k in [k for k in sys.modules if k.startswith("abs_tools")]:
        del sys.modules[k]
    sys.path[:] = [p for p in sys.path if "abs_tools" not in p]


class TestAbsoluteImportDiagnostic:
    def test_register_warns_on_absolute_import(
        self, tool_store_absolute_imports, caplog
    ) -> None:
        reg = ToolRegistry(store_path=tool_store_absolute_imports)
        with caplog.at_level("WARNING", logger="bioimageflow"):
            metas = reg.register_package("abs_tools", "1.0.0")

        # No tools register, but the warning explains why.
        assert metas == []
        assert any(
            "AlphaTool" in rec.message
            and "absolute imports" in rec.message
            and "abs_tools" in rec.message
            for rec in caplog.records
        ), f"expected absolute-import warning, got: {caplog.records}"
