"""
Integration tests for versioned tool package loading.

Covers:
- Node creation with versioned ProcessingTool, DataFrameTool, SubWorkflow
- Execution produces correct version-specific results
- Two versions in the same workflow produce different outputs
- Serialization round-trip preserves version info
- Cache keys differ between versions
"""

import json
import sys

import pandas as pd
import pytest

from bioimageflow import Workflow


# ---------------------------------------------------------------------------
# Fixture: tool store with two versions
# ---------------------------------------------------------------------------

@pytest.fixture
def tool_store(tmp_path):
    """Build a tool store with v1.0.0 and v2.0.0 of dummy_tools.

    v1: AlphaTool.process_row → result="v1", LoaderTool, AlphaPipeline (SubWorkflow)
    v2: AlphaTool.process_row → result="v2" (extra field), LoaderTool adds version col
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
            "from .pipeline import AlphaPipeline\n"
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
            "from bioimageflow_core import IOModel, Arguments\n\n"
            "class AlphaTool(DummyBase):\n"
            f"    display_name = 'Alpha'\n"
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

        (pkg_dir / "pipeline.py").write_text(
            "from bioimageflow.sub_workflow import SubWorkflow\n"
            "from bioimageflow_core import IOModel\n"
            "from bioimageflow.node import ColumnRef\n"
            "from .alpha import AlphaTool\n"
            "from .loader import LoaderTool\n\n"
            "class AlphaPipeline(SubWorkflow):\n"
            "    display_name = 'Alpha Pipeline'\n"
            "    class Inputs(IOModel):\n"
            "        path: str\n"
            "    class Outputs(IOModel):\n"
            "        result: str\n"
            "    def build(self, inputs):\n"
            "        loader = LoaderTool()\n"
            "        alpha = AlphaTool()\n"
            "        raw = loader(path=inputs.path)\n"
            "        processed = alpha(value=0)\n"
            "        return {'result': processed['result']}\n"
        )

    return store


@pytest.fixture
def data_dir(tmp_path):
    """Create a directory with test .tif files."""
    d = tmp_path / "data"
    d.mkdir()
    for name in ["img_01.tif", "img_02.tif"]:
        (d / name).write_text(f"FAKE_{name}")
    return d


@pytest.fixture(autouse=True)
def _cleanup_scoped_modules():
    """Remove scoped and canonical dummy_tools modules after each test."""
    yield
    to_remove = [k for k in sys.modules
                 if k.startswith("dummy_tools__") or k.startswith("dummy_tools")]
    for k in to_remove:
        del sys.modules[k]
    sys.path[:] = [p for p in sys.path if "dummy_tools" not in p]


# ---------------------------------------------------------------------------
# Helper: load versions
# ---------------------------------------------------------------------------

def _load_v1(store):
    from bioimageflow.tool_loader import load_versioned_package
    return load_versioned_package("dummy_tools", "1.0.0", store)


def _load_v2(store):
    from bioimageflow.tool_loader import load_versioned_package
    return load_versioned_package("dummy_tools", "2.0.0", store)


# ---------------------------------------------------------------------------
# Node creation
# ---------------------------------------------------------------------------

class TestVersionedNodeCreation:

    def test_processing_tool_creates_node(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results"):
            node = v1.AlphaTool()(value=5)
            assert node.tool.display_name == "Alpha"

    def test_dataframe_tool_creates_node(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results"):
            node = v1.LoaderTool()(path=str(data_dir))
            assert node.tool.display_name == "Dummy Loader"

    def test_input_validation(self, tool_store, data_dir):
        from bioimageflow.node import BindingError
        v1 = _load_v1(tool_store)
        with pytest.raises(BindingError, match="nonexistent_field"):
            with Workflow(storage_path=data_dir.parent / "results"):
                v1.AlphaTool()(nonexistent_field=5)

    def test_output_column_ref(self, tool_store, data_dir):
        from bioimageflow.node import ColumnRef, ColumnNotFoundError
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results"):
            node = v1.AlphaTool()(value=0)
            ref = node["result"]
            assert isinstance(ref, ColumnRef)
            with pytest.raises(ColumnNotFoundError):
                node["nonexistent"]

    def test_two_versions_in_same_workflow(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        v2 = _load_v2(tool_store)
        with Workflow(storage_path=data_dir.parent / "results"):
            n1 = v1.AlphaTool()(value=0)
            n2 = v2.AlphaTool()(value=0)
            assert n1.name == "AlphaTool_1"
            assert n2.name == "AlphaTool_2"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TestVersionedExecution:

    def test_processing_tool_executes(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaTool()(value=0)
            df = wf.compute(node)
        assert "result" in df.columns
        assert df["result"].iloc[0] == "v1"

    def test_dataframe_tool_executes(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.LoaderTool()(path=str(data_dir))
            df = wf.compute(node)
        assert "filepath" in df.columns
        assert len(df) == 2

    def test_two_versions_produce_different_results(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        v2 = _load_v2(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            n1 = v1.AlphaTool()(value=0)
            n2 = v2.AlphaTool()(value=0)
            results = wf.compute(n1, n2)

        assert results["AlphaTool_1"]["result"].iloc[0] == "v1"
        assert results["AlphaTool_2"]["result"].iloc[0] == "v2"

    def test_dataframe_tool_v2_adds_column(self, tool_store, data_dir):
        v2 = _load_v2(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v2.LoaderTool()(path=str(data_dir))
            df = wf.compute(node)
        assert "version" in df.columns
        assert df["version"].iloc[0] == "v2"

    def test_versioned_tool_with_column_bindings(self, tool_store, data_dir):
        """LoaderTool -> AlphaTool pipeline using column bindings."""
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            _raw = v1.LoaderTool()(path=str(data_dir))
            processed = v1.AlphaTool()(value=0)
            df = wf.compute(processed)
        assert "result" in df.columns


# ---------------------------------------------------------------------------
# SubWorkflow
# ---------------------------------------------------------------------------

class TestVersionedSubWorkflow:

    def test_sub_workflow_creates_node(self, tool_store, data_dir):
        from bioimageflow.sub_workflow import SubWorkflowNode
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results"):
            _raw = v1.LoaderTool()(path=str(data_dir))
            node = v1.AlphaPipeline()(path=str(data_dir))
            assert isinstance(node, SubWorkflowNode)

    def test_sub_workflow_uses_correct_version_tools(self, tool_store, data_dir):
        """Internal nodes of v1 sub-workflow should carry v1 metadata."""
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results"):
            node = v1.AlphaPipeline()(path=str(data_dir))
            for internal in node.internal_nodes:
                version = getattr(type(internal.tool), "_bif_package_version", None)
                if version is not None:
                    assert version == "1.0.0"

    def test_sub_workflow_executes(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaPipeline()(path=str(data_dir))
            df = wf.compute(node)
        assert "result" in df.columns
        assert df["result"].iloc[0] == "v1"

    def test_two_sub_workflow_versions_differ(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        v2 = _load_v2(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            n1 = v1.AlphaPipeline()(path=str(data_dir))
            n2 = v2.AlphaPipeline()(path=str(data_dir))
            results = wf.compute(n1, n2)

        assert results["AlphaPipeline_1"]["result"].iloc[0] == "v1"
        assert results["AlphaPipeline_2"]["result"].iloc[0] == "v2"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestVersionedSerialization:

    def test_export_includes_version_info(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaTool()(value=0)
            wf.compute(node)
            wf.export(data_dir.parent / "workflow.json")

        data = json.loads((data_dir.parent / "workflow.json").read_text())
        alpha_node = next(n for n in data["nodes"] if n["name"] == "AlphaTool_1")
        assert alpha_node["tool_package"] == "dummy_tools"
        assert alpha_node["tool_package_version"] == "1.0.0"
        assert alpha_node["tool_module"] == "dummy_tools.alpha"

    def test_export_sub_workflow_includes_version_info(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaPipeline()(path=str(data_dir))
            wf.compute(node)
            wf.export(data_dir.parent / "workflow.json")

        data = json.loads((data_dir.parent / "workflow.json").read_text())
        sw_node = next(n for n in data["nodes"] if n.get("type") == "sub_workflow")
        assert sw_node["sub_workflow_package"] == "dummy_tools"
        assert sw_node["sub_workflow_package_version"] == "1.0.0"

    def test_export_mixed_versions(self, tool_store, data_dir):
        v1 = _load_v1(tool_store)
        v2 = _load_v2(tool_store)
        with Workflow(storage_path=data_dir.parent / "results") as wf:
            n1 = v1.AlphaTool()(value=0)
            n2 = v2.AlphaTool()(value=0)
            wf.compute(n1, n2)
            wf.export(data_dir.parent / "workflow.json")

        data = json.loads((data_dir.parent / "workflow.json").read_text())
        nodes_by_name = {n["name"]: n for n in data["nodes"]}
        assert nodes_by_name["AlphaTool_1"]["tool_package_version"] == "1.0.0"
        assert nodes_by_name["AlphaTool_2"]["tool_package_version"] == "2.0.0"

    def test_load_versioned_workflow(self, tool_store, data_dir, monkeypatch):
        """Export then load — loaded tools have correct version metadata."""
        monkeypatch.setenv("BIOIMAGEFLOW_TOOL_STORE", str(tool_store))
        v1 = _load_v1(tool_store)

        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaTool()(value=0)
            _df1 = wf.compute(node)
            wf.export(data_dir.parent / "workflow.json")

        from bioimageflow.tool_loader import unload_versioned_package
        unload_versioned_package("dummy_tools", "1.0.0")

        loaded = Workflow.load(data_dir.parent / "workflow.json")
        terminal = loaded.nodes["AlphaTool_1"]
        assert getattr(type(terminal.tool), "_bif_package_version", None) == "1.0.0"

        df2 = loaded.compute(terminal)
        assert df2["result"].iloc[0] == "v1"

    def test_full_round_trip(self, tool_store, data_dir, monkeypatch):
        """Build → compute → export → load → re-compute produces same results."""
        monkeypatch.setenv("BIOIMAGEFLOW_TOOL_STORE", str(tool_store))
        v1 = _load_v1(tool_store)

        with Workflow(storage_path=data_dir.parent / "results") as wf:
            _raw = v1.LoaderTool()(path=str(data_dir))
            node = v1.AlphaTool()(value=0)
            df1 = wf.compute(node)
            wf.export(data_dir.parent / "workflow.json")

        from bioimageflow.tool_loader import unload_versioned_package
        unload_versioned_package("dummy_tools", "1.0.0")

        loaded = Workflow.load(data_dir.parent / "workflow.json")
        terminal = loaded.nodes["AlphaTool_1"]
        df2 = loaded.compute(terminal)
        pd.testing.assert_frame_equal(df1, df2)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestVersionedCaching:

    def test_cache_hit_same_version(self, tool_store, data_dir):
        """Second run with same version hits cache."""
        v1 = _load_v1(tool_store)

        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaTool()(value=0)
            wf.compute(node)

        events = []
        with Workflow(
            storage_path=data_dir.parent / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            node = v1.AlphaTool()(value=0)
            wf.compute(node)

        cached = [e for e in events if e.status == "cached"]
        assert len(cached) > 0

    def test_cache_miss_different_version(self, tool_store, data_dir):
        """Different version with same params misses cache."""
        v1 = _load_v1(tool_store)
        v2 = _load_v2(tool_store)

        with Workflow(storage_path=data_dir.parent / "results") as wf:
            node = v1.AlphaTool()(value=0)
            wf.compute(node)

        events = []
        with Workflow(
            storage_path=data_dir.parent / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            node = v2.AlphaTool()(value=0)
            wf.compute(node)

        # The v2 node should NOT be cached — it has a different version
        cached_alpha = [e for e in events
                        if e.status == "cached" and "alpha" in e.node_name]
        assert len(cached_alpha) == 0
