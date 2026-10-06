"""Focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect
from pathlib import Path

import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec

from bioimageflow.dataframe_tool import DataFrameTool


pytest_plugins = ("tests.testkit.tool_loader",)


@pytest.mark.parametrize("install_dependencies", [True, False])
def test_ensure_installed_dependency_option(
    tmp_path, monkeypatch, install_dependencies: bool
) -> None:
    from bioimageflow import tool_loader

    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        commands.append(command)
        from tests.testkit.tool_loader import record_distribution

        target = Path(command[command.index("--target") + 1])
        package = target / "example_tools"
        package.mkdir()
        (package / "__init__.py").write_text("")
        record_distribution(target, "example_tools", "1.2.3")

    monkeypatch.setattr(tool_loader.subprocess, "run", fake_run)
    tool_loader.ensure_installed(
        "example_tools",
        "1.2.3",
        "example-tools",
        tmp_path,
        install_dependencies=install_dependencies,
    )

    assert len(commands) == 1
    assert commands[0][:4] == [sys.executable, "-m", "pip", "install"]
    assert commands[0][-1] == "example-tools==1.2.3"
    assert ("--no-deps" in commands[0]) is not install_dependencies


def test_ensure_installed_default_installs_dependencies(tmp_path, monkeypatch) -> None:
    from bioimageflow import tool_loader

    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        commands.append(command)
        from tests.testkit.tool_loader import record_distribution

        target = Path(command[command.index("--target") + 1])
        package = target / "example_tools"
        package.mkdir()
        (package / "__init__.py").write_text("")
        record_distribution(target, "example_tools", "1.2.3")

    monkeypatch.setattr(tool_loader.subprocess, "run", fake_run)
    tool_loader.ensure_installed("example_tools", "1.2.3", "example-tools", tmp_path)

    assert "--no-deps" not in commands[0]


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
    def test_dependency_import_restores_caller_sys_path(self, tool_store):
        """Selected dependencies resolve during loading without persistent paths."""
        from bioimageflow.tool_loader import load_versioned_package

        before = list(sys.path)
        package = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert package.AlphaTool.process_row(package.AlphaTool(), None).result == "v1"
        assert sys.modules["dep_pkg"].DEP_VALUE == 42
        assert sys.path == before

    def test_store_dir_removed_on_unload(self, tool_store):
        from bioimageflow.tool_loader import (
            load_versioned_package,
            unload_versioned_package,
        )

        load_versioned_package("dummy_tools", "1.0.0", tool_store)
        expected = str(tool_store / "dummy_tools" / "1.0.0")
        assert expected not in sys.path

        unload_versioned_package("dummy_tools", "1.0.0")
        assert expected not in sys.path

    @pytest.mark.compat
    def test_worker_loads_versioned_package_tool_with_relative_imports(
        self, tool_store
    ):
        from bioimageflow.tool_loader import load_versioned_package
        from bioimageflow.worker_origins import resolve_worker_tool_origin
        from bioimageflow_core import VersionedModuleOrigin
        from bioimageflow_core.worker_origins import load_worker_tool

        package = load_versioned_package("dummy_tools", "1.0.0", tool_store)
        origin = resolve_worker_tool_origin(package.AlphaTool)
        assert isinstance(origin, VersionedModuleOrigin)
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


def test_direct_late_dependency_import_and_cached_reuse_preserve_paths(tool_store, tmp_path):
    from bioimageflow import Workflow, WorkflowExecutionContext
    from bioimageflow.tool_loader import load_versioned_package
    from tests.testkit.tool_loader import record_distribution

    root = tool_store / "dummy_tools" / "1.0.0"
    alpha = root / "dummy_tools" / "alpha.py"
    alpha.write_text(alpha.read_text().replace("import dep_pkg\n", "").replace(
        "        return self.Outputs(result='v1')",
        "        import dep_pkg\n        self.calls.append(dep_pkg.DEP_VALUE)\n        return self.Outputs(result=str(dep_pkg.DEP_VALUE))",
    ))
    alpha.write_text(alpha.read_text().replace("    display_name", "    calls = []\n    display_name"))
    record_distribution(root, "dummy_tools", "1.0.0")
    before = list(sys.path)
    package = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    assert "dep_pkg" not in sys.modules
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = package.AlphaTool()(name="late")
    engine = workflow.create_engine()
    first_context = WorkflowExecutionContext()
    first = workflow.compute(node, engine=engine, run_context=first_context)
    assert first["result"].tolist() == ["42"]
    assert sys.path == before
    second_context = WorkflowExecutionContext()
    second = workflow.compute(node, engine=engine, run_context=second_context)
    assert second["result"].tolist() == ["42"]
    [first_outcome] = first_context.execution_outcomes
    [second_outcome] = second_context.execution_outcomes
    assert package.AlphaTool.calls == [42]
    assert (first_outcome.result_key, first_outcome.record_id) == (second_outcome.result_key, second_outcome.record_id)
    assert sys.path == before
    engine.close()
    workflow.shared_memory_context.close()


def test_unload_preserves_foreign_same_suffix_caller_path(tool_store, tmp_path, monkeypatch):
    from bioimageflow.tool_loader import load_versioned_package, unload_versioned_package

    foreign = tmp_path / "foreign" / "dummy_tools" / "1.0.0"
    monkeypatch.syspath_prepend(str(foreign))
    before = list(sys.path)
    selected = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    assert selected.AlphaTool().process_row(None).result == "v1"
    unload_versioned_package("dummy_tools", "1.0.0")
    assert sys.path == before


def test_versioned_dataframe_late_dependency_is_admitted_before_cached_reuse(tool_store, tmp_path):
    from bioimageflow import Workflow, WorkflowExecutionContext
    from bioimageflow.tool_loader import load_versioned_package
    from tests.testkit.tool_loader import record_distribution

    root = tool_store / "dummy_tools" / "1.0.0"
    source = root / "dummy_tools" / "loader.py"
    source.write_text(source.read_text().replace("    display_name", "    calls = []\n    display_name").replace(
        "        return df", "        import dep_pkg\n        self.calls.append(dep_pkg.DEP_VALUE)\n        df['filepath'] = str(dep_pkg.DEP_VALUE)\n        return df",
    ))
    record_distribution(root, "dummy_tools", "1.0.0")
    package = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    files = tmp_path / "files"
    files.mkdir()
    (files / "actual.tif").write_bytes(b"fixture")
    before = list(sys.path)
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = package.LoaderTool()(path=str(files), name="dataframe")
    engine = workflow.create_engine()
    try:
        contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
        frames = [workflow.compute(node, engine=engine, run_context=context) for context in contexts]
        assert [frame['filepath'].tolist() for frame in frames] == [["42"], ["42"]]
        assert package.LoaderTool.calls == [42]
        first, second = (context.execution_outcomes[0] for context in contexts)
        assert (first.result_key, first.record_id) == (second.result_key, second.record_id)
        assert sys.path == before
    finally:
        engine.close()
        workflow.shared_memory_context.close()


def test_public_canonical_alias_refuses_foreign_module_without_partial_aliases(tool_store, tmp_path, monkeypatch):
    from types import ModuleType
    from bioimageflow.tool_loader import require_tool_packages

    foreign = ModuleType("dummy_tools")
    foreign.sentinel = object()
    monkeypatch.setitem(sys.modules, "dummy_tools", foreign)
    script = tmp_path / "workflow.py"
    script.write_text('# /// script\n# dependencies = ["dummy-tools==1.0.0"]\n# ///\n')
    before = list(sys.path)
    with pytest.raises(ImportError, match="Canonical tool alias.*foreign owner"):
        require_tool_packages(script, store_path=tool_store, auto_install=False)
    assert sys.modules["dummy_tools"] is foreign
    assert "dummy_tools.alpha" not in sys.modules
    assert sys.modules["dummy_tools__1_0_0"].AlphaTool().process_row(None).result == "v1"
    assert sys.path == before
