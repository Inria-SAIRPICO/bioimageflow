"""
Test config-driven sub-workflows (SubWorkflow.from_config).

Covers:
- _build_iomodel helper: basic types, ImageSpec, instantiation
- _resolve_node_input helper: from_input, from_node, raw constants
- SubWorkflow.from_config factory
- Single-node and multi-node execution
- Default values and raw constants in node inputs
- Encapsulation (internal nodes not in parent)
- Scoped names in compute_steps
- Caching
- Nested config-driven sub-workflows
- Nested class-based sub-workflows inside config
- Serialization round-trip
- Multiple instances of same config
- Equivalence with class-based sub-workflows
- Error cases
"""

from pathlib import Path
from typing import Annotated, get_args, get_origin
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.node import BindingError
from bioimageflow.sub_workflow import (
    SubWorkflow,
    _build_iomodel,
    _resolve_node_input,
)
from bioimageflow_core import ImageSpec, Semantic

from .conftest import FileLoader


# ---------------------------------------------------------------------------
# Test helpers — config factories
# ---------------------------------------------------------------------------

def _single_seg_config():
    """Config mirroring SegmentOnly: FileLoader → StubSegmenter."""
    return {
        "name": "config_segment",
        "inputs": {
            "image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
        },
        "outputs": {
            "mask": {"type": "Path", "image_spec": {"semantics": ["label"]}},
            "cell_count": {"type": "int"},
        },
        "nodes": [
            {
                "name": "seg",
                "tool_class": "StubSegmenter",
                "tool_module": "tests.integration.conftest",
                "inputs": {
                    "input_image": {"from_input": "image"},
                },
            },
        ],
        "output_mapping": {
            "mask": {"from_node": "seg", "column": "mask"},
            "cell_count": {"from_node": "seg", "column": "cell_count"},
        },
    }


def _two_node_config():
    """Config mirroring SegmentAndMeasure: StubSegmenter → StubStats."""
    return {
        "name": "config_seg_measure",
        "inputs": {
            "image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
            "diameter": {"type": "float", "default": 30.0},
        },
        "outputs": {
            "mask": {"type": "Path", "image_spec": {"semantics": ["label"]}},
            "cell_count": {"type": "int"},
            "mean_intensity": {"type": "float"},
            "area": {"type": "int"},
        },
        "nodes": [
            {
                "name": "seg",
                "tool_class": "StubSegmenter",
                "tool_module": "tests.integration.conftest",
                "inputs": {
                    "input_image": {"from_input": "image"},
                    "diameter": {"from_input": "diameter"},
                },
            },
            {
                "name": "stats",
                "tool_class": "StubStats",
                "tool_module": "tests.integration.conftest",
                "inputs": {
                    "image": {"from_input": "image"},
                    "mask": {"from_node": "seg", "column": "mask"},
                },
            },
        ],
        "output_mapping": {
            "mask": {"from_node": "seg", "column": "mask"},
            "cell_count": {"from_node": "seg", "column": "cell_count"},
            "mean_intensity": {"from_node": "stats", "column": "mean_intensity"},
            "area": {"from_node": "stats", "column": "area"},
        },
    }


# ===========================================================================
# Unit tests — _build_iomodel
# ===========================================================================

class TestBuildIOModel:

    def test_basic_types(self):
        """Creates IOModel with correct annotations and defaults."""
        cls = _build_iomodel("TestIO", {
            "x": {"type": "int"},
            "y": {"type": "float", "default": 3.14},
            "name": {"type": "str", "default": "hello"},
            "flag": {"type": "bool", "default": True},
        })
        ann = cls._get_all_annotations()
        assert ann["x"] is int
        assert ann["y"] is float
        assert ann["name"] is str
        assert ann["flag"] is bool
        assert cls.y == 3.14  # type: ignore[attr-defined]
        assert cls.name == "hello"  # type: ignore[attr-defined]
        assert cls.flag is True  # type: ignore[attr-defined]
        assert not hasattr(cls, "x")

    def test_path_with_image_spec(self):
        """Path fields with image_spec produce Annotated[Path, ImageSpec]."""
        cls = _build_iomodel("ImgIO", {
            "image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
        })
        ann = cls._get_all_annotations()
        assert get_origin(ann["image"]) is Annotated
        base, spec = get_args(ann["image"])
        assert base is Path
        assert isinstance(spec, ImageSpec)
        assert Semantic.INTENSITY in spec.semantics

    def test_path_with_null_image_spec_is_plain_path(self):
        """Path fields with null image_spec stay plain Path."""
        cls = _build_iomodel("PathIO", {
            "image": {"type": "Path", "image_spec": None},
        })
        ann = cls._get_all_annotations()
        assert ann["image"] is Path

    def test_image_file_with_missing_image_spec(self):
        """ImageFile fields with missing image_spec use an empty ImageSpec."""
        cls = _build_iomodel("ImgIO", {
            "image": {"type": "ImageFile"},
        })
        ann = cls._get_all_annotations()
        assert get_origin(ann["image"]) is Annotated
        base, spec = get_args(ann["image"])
        assert base is Path
        assert isinstance(spec, ImageSpec)
        assert spec.semantics == frozenset()
        assert spec.layouts == frozenset()

    def test_image_file_with_null_image_spec(self):
        """ImageFile fields with null image_spec use an empty ImageSpec."""
        cls = _build_iomodel("ImgIO", {
            "image": {"type": "ImageFile", "image_spec": None},
        })
        ann = cls._get_all_annotations()
        assert get_origin(ann["image"]) is Annotated
        base, spec = get_args(ann["image"])
        assert base is Path
        assert isinstance(spec, ImageSpec)
        assert spec.semantics == frozenset()
        assert spec.layouts == frozenset()

    def test_image_spec_must_be_dict_or_null(self):
        """Malformed falsey image_spec values are not silently accepted."""
        with pytest.raises(TypeError, match="image_spec must be a dict or null"):
            _build_iomodel("BadIO", {
                "image": {"type": "ImageFile", "image_spec": []},
            })

    def test_image_spec_with_layouts(self):
        """image_spec with layouts produces correct ImageSpec."""
        cls = _build_iomodel("LayoutIO", {
            "vol": {"type": "Path", "image_spec": {
                "semantics": ["label"],
                "layouts": ["ZYX"],
            }},
        })
        ann = cls._get_all_annotations()
        _, spec = get_args(ann["vol"])
        assert Semantic.LABEL in spec.semantics
        from bioimageflow_core import Layout
        assert Layout.VOLUMETRIC in spec.layouts

    def test_instantiation(self):
        """Dynamic IOModel can be instantiated and validates fields."""
        cls = _build_iomodel("ValIO", {
            "x": {"type": "int"},
            "y": {"type": "float", "default": 1.0},
        })
        obj = cls(x=42)
        assert obj.x == 42  # type: ignore[attr-defined]
        assert obj.y == 1.0  # type: ignore[attr-defined]
        with pytest.raises(TypeError, match="Missing required"):
            cls()  # x is required

    def test_empty_fields(self):
        """Empty fields config produces IOModel with no annotations."""
        cls = _build_iomodel("EmptyIO", {})
        assert cls._get_all_annotations() == {}
        obj = cls()  # should work with no fields
        assert obj is not None


# ===========================================================================
# Unit tests — _resolve_node_input
# ===========================================================================

class TestResolveNodeInput:

    def test_from_input_ref(self):
        """from_input ref resolves via proxy subscript."""
        proxy = MagicMock()
        proxy.__getitem__ = MagicMock(return_value="PROXY_REF")
        result = _resolve_node_input({"from_input": "image"}, proxy, {})
        proxy.__getitem__.assert_called_once_with("image")
        assert result == "PROXY_REF"

    def test_from_node_ref(self):
        """from_node ref resolves from built_nodes dict."""
        mock_node = MagicMock()
        mock_node.__getitem__ = MagicMock(return_value="NODE_COL_REF")
        result = _resolve_node_input(
            {"from_node": "seg", "column": "mask"},
            MagicMock(),
            {"seg": mock_node},
        )
        mock_node.__getitem__.assert_called_once_with("mask")
        assert result == "NODE_COL_REF"

    def test_raw_int(self):
        assert _resolve_node_input(42, MagicMock(), {}) == 42

    def test_raw_float(self):
        assert _resolve_node_input(3.14, MagicMock(), {}) == 3.14

    def test_raw_string(self):
        assert _resolve_node_input("hello", MagicMock(), {}) == "hello"

    def test_raw_bool(self):
        assert _resolve_node_input(True, MagicMock(), {}) is True

    def test_raw_list(self):
        assert _resolve_node_input([1, 2, 3], MagicMock(), {}) == [1, 2, 3]


# ===========================================================================
# Unit tests — SubWorkflow.from_config
# ===========================================================================

class TestFromConfig:

    def test_returns_subworkflow(self):
        """from_config returns a SubWorkflow with correct name/Inputs/Outputs."""
        config = {
            "name": "test_sw",
            "inputs": {"x": {"type": "int"}},
            "outputs": {"y": {"type": "int"}},
            "nodes": [],
            "output_mapping": {"y": {"from_node": "source", "column": "y"}},
        }
        sw = SubWorkflow.from_config(config)
        assert isinstance(sw, SubWorkflow)
        assert sw.display_name == "test_sw"
        assert sw.Inputs is not None
        assert sw.Outputs is not None
        assert sw.Inputs._get_all_annotations()["x"] is int
        assert sw.Outputs._get_all_annotations()["y"] is int

    def test_preserves_config(self):
        """from_config stores the config dict for serialization."""
        config = _single_seg_config()
        sw = SubWorkflow.from_config(config)
        assert sw._config == config  # type: ignore[attr-defined]

    def test_gui_interface_accepts_image_file_alias_and_null_path_spec(self):
        """GUI-style ImageFile and null Path image_spec build valid interfaces."""
        config = {
            "name": "gui_interface",
            "inputs": {
                "image": {"type": "Path", "image_spec": None},
            },
            "outputs": {
                "mask": {"type": "ImageFile", "image_spec": None},
            },
            "nodes": [],
            "output_mapping": {
                "mask": {"from_node": "seg", "column": "mask"},
            },
        }

        sw = SubWorkflow.from_config(config)

        input_ann = sw.Inputs._get_all_annotations()["image"]
        output_ann = sw.Outputs._get_all_annotations()["mask"]  # type: ignore[union-attr]
        assert input_ann is Path
        assert get_origin(output_ann) is Annotated
        base, spec = get_args(output_ann)
        assert base is Path
        assert isinstance(spec, ImageSpec)
        assert spec.semantics == frozenset()
        assert spec.layouts == frozenset()


# ===========================================================================
# Integration tests — execution
# ===========================================================================

class TestConfigSubWorkflowExecution:

    def test_single_node_execution(self, tmp_workspace):
        """Config SW with one internal node produces correct DataFrame."""
        sw = SubWorkflow.from_config(_single_seg_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            df = wf.compute(results)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert "mask" in df.columns
        assert "cell_count" in df.columns

    def test_two_node_chain(self, tmp_workspace):
        """Two chained nodes (seg → stats) with inter-node refs."""
        sw = SubWorkflow.from_config(_two_node_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"], diameter=25.0)
            df = wf.compute(results)

        assert len(df) == 3
        assert set(df.columns) == {"mask", "cell_count", "mean_intensity", "area"}

    def test_default_values_used(self, tmp_workspace):
        """Caller omits optional input — default from config used."""
        sw = SubWorkflow.from_config(_two_node_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            # diameter not provided — should use default 30.0
            results = sw(image=raw["path"])
            df = wf.compute(results)

        assert len(df) == 3

    def test_raw_constant_in_node_input(self, tmp_workspace):
        """Node input is a raw constant (not a ref)."""
        config = _single_seg_config()
        # Add a raw constant for diameter in the seg node
        config["nodes"][0]["inputs"]["diameter"] = 42.0
        sw = SubWorkflow.from_config(config)
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            df = wf.compute(results)

        assert len(df) == 3


# ===========================================================================
# Integration tests — encapsulation
# ===========================================================================

class TestConfigSubWorkflowEncapsulation:

    def test_internal_nodes_not_in_parent(self, tmp_workspace):
        """Config SW internal nodes don't leak into parent workflow."""
        sw = SubWorkflow.from_config(_single_seg_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            _results = sw(image=raw["path"])

            # Internal node "seg" should not appear directly
            assert not any("StubSegmenter" in k for k in wf.nodes)
            # But the sub-workflow node itself should
            assert any("_ConfigDrivenSubWorkflow" in k for k in wf.nodes)


# ===========================================================================
# Integration tests — compute_steps scoped names
# ===========================================================================

class TestConfigSubWorkflowComputeSteps:

    def test_scoped_names(self, tmp_workspace):
        """Internal nodes have scoped names in compute_steps."""
        sw = SubWorkflow.from_config(_single_seg_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            names = []
            for step in wf.compute_steps(results):
                names.append(step.node_name)
                step.execute()

        assert any("_ConfigDrivenSubWorkflow_1/" in n for n in names)


# ===========================================================================
# Integration tests — caching
# ===========================================================================

class TestConfigSubWorkflowCaching:

    def test_cache_hit_on_second_run(self, tmp_workspace):
        """Second execution hits cache for internal nodes."""
        sw = SubWorkflow.from_config(_single_seg_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            wf.compute(results)

        events = []
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            wf.compute(results)

        cached = [e for e in events if getattr(e, "status", None) == "cached"]
        assert len(cached) > 0


# ===========================================================================
# Integration tests — nested sub-workflows
# ===========================================================================

class TestConfigSubWorkflowNested:

    def test_nested_config(self, tmp_workspace):
        """A config node can contain another config sub-workflow."""
        inner_config = _single_seg_config()
        outer_config = {
            "name": "outer_config",
            "inputs": {
                "image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
            },
            "outputs": {
                "mask": {"type": "Path", "image_spec": {"semantics": ["label"]}},
                "cell_count": {"type": "int"},
            },
            "nodes": [
                {
                    "name": "inner",
                    "type": "sub_workflow",
                    "config": inner_config,
                    "inputs": {
                        "image": {"from_input": "image"},
                    },
                },
            ],
            "output_mapping": {
                "mask": {"from_node": "inner", "column": "mask"},
                "cell_count": {"from_node": "inner", "column": "cell_count"},
            },
        }
        sw = SubWorkflow.from_config(outer_config)
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            df = wf.compute(results)

        assert len(df) == 3
        assert "mask" in df.columns

    def test_class_based_nested_in_config(self, tmp_workspace):
        """A config node can reference a class-based SubWorkflow."""

        outer_config = {
            "name": "outer_with_class",
            "inputs": {
                "image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
            },
            "outputs": {
                "mask": {"type": "Path", "image_spec": {"semantics": ["label"]}},
                "cell_count": {"type": "int"},
            },
            "nodes": [
                {
                    "name": "inner",
                    "type": "sub_workflow",
                    "sub_workflow_class": "SegmentOnly",
                    "sub_workflow_module": "tests.integration.test_sub_workflow",
                    "inputs": {
                        "image": {"from_input": "image"},
                    },
                },
            ],
            "output_mapping": {
                "mask": {"from_node": "inner", "column": "mask"},
                "cell_count": {"from_node": "inner", "column": "cell_count"},
            },
        }
        sw = SubWorkflow.from_config(outer_config)
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            df = wf.compute(results)

        assert len(df) == 3
        assert "mask" in df.columns


# ===========================================================================
# Integration tests — serialization
# ===========================================================================

class TestConfigSubWorkflowSerialization:

    def test_export_load_round_trip(self, tmp_workspace):
        """Config SW can be serialized and deserialized."""
        sw = SubWorkflow.from_config(_two_node_config())
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = sw(image=raw["path"])
            df1 = wf.compute(results)
            wf.export(tmp_workspace / "workflow.json")

        wf2 = Workflow.load(tmp_workspace / "workflow.json")
        terminal = [n for n in wf2.nodes if "_ConfigDrivenSubWorkflow" in n]
        assert len(terminal) == 1
        df2 = wf2.compute(wf2.nodes[terminal[0]])

        assert set(df1.columns) == set(df2.columns)
        assert len(df1) == len(df2)


# ===========================================================================
# Integration tests — multiple instances
# ===========================================================================

class TestConfigSubWorkflowMultiple:

    def test_two_instances_same_config(self, tmp_workspace):
        """Same config used twice with different params."""
        config = _two_node_config()
        sw = SubWorkflow.from_config(config)
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            r1 = sw(image=raw["path"], diameter=20.0)
            r2 = sw(image=raw["path"], diameter=50.0)
            df1 = wf.compute(r1)
            df2 = wf.compute(r2)

        assert len(df1) == 3
        assert len(df2) == 3


# ===========================================================================
# Integration tests — equivalence with class-based
# ===========================================================================

class TestConfigSubWorkflowEquivalence:

    def test_matches_class_based_results(self, tmp_workspace):
        """Config SW produces same results as equivalent class-based."""
        from .test_sub_workflow import SegmentAndMeasure

        sw_config = SubWorkflow.from_config(_two_node_config())
        sw_class = SegmentAndMeasure()
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            r1 = sw_config(image=raw["path"])
            df1 = wf.compute(r1)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results2") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            r2 = sw_class(image=raw["path"])
            df2 = wf.compute(r2)

        assert set(df1.columns) == set(df2.columns)
        assert len(df1) == len(df2)
        # Non-path columns should have identical values
        for col in df1.columns:
            if col == "mask":
                continue  # paths differ due to different sub-workflow names
            assert list(df1[col]) == list(df2[col])


# ===========================================================================
# Error tests
# ===========================================================================

class TestConfigSubWorkflowErrors:

    def test_missing_required_input_raises(self, tmp_workspace):
        """Missing required config input raises BindingError."""
        sw = SubWorkflow.from_config(_single_seg_config())

        with pytest.raises(BindingError):
            with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
                sw()  # 'image' is required, not provided

    def test_unknown_input_raises(self, tmp_workspace):
        """Unknown keyword arg raises BindingError."""
        sw = SubWorkflow.from_config(_single_seg_config())
        load = FileLoader()

        with pytest.raises(BindingError):
            with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                sw(image=raw["path"], nonexistent=42)

    def test_invalid_type_string_raises(self):
        """Invalid type string in config raises KeyError."""
        config = {
            "name": "bad",
            "inputs": {"x": {"type": "ComplexNumber"}},
            "outputs": {},
            "nodes": [],
            "output_mapping": {},
        }
        with pytest.raises(KeyError):
            SubWorkflow.from_config(config)

    def test_from_input_must_reference_declared_input(self):
        """Published input refs must point at the config input interface."""
        config = _single_seg_config()
        config["nodes"][0]["inputs"]["input_image"] = {"from_input": "missing_image"}

        with pytest.raises(ValueError, match="undeclared input 'missing_image'"):
            SubWorkflow.from_config(config)

    def test_declared_outputs_require_output_mapping(self):
        """Every published output must map to an internal node output."""
        config = _single_seg_config()
        del config["output_mapping"]["mask"]

        with pytest.raises(ValueError, match="missing output_mapping entries"):
            SubWorkflow.from_config(config)

    def test_output_mapping_cannot_publish_undeclared_output(self):
        """The output mapping is exactly the published output interface."""
        config = _single_seg_config()
        config["output_mapping"]["extra"] = {"from_node": "seg", "column": "mask"}

        with pytest.raises(ValueError, match="undeclared output 'extra'"):
            SubWorkflow.from_config(config)

    def test_output_mapping_entries_must_have_node_and_column(self):
        """Published outputs must point to an internal node column reference."""
        config = _single_seg_config()
        config["output_mapping"]["mask"] = {"from_node": "seg"}

        with pytest.raises(ValueError, match="from_node.*column"):
            SubWorkflow.from_config(config)

    def test_output_mapping_must_be_dict(self):
        """The published output mapping must be a mapping object."""
        config = _single_seg_config()
        config["output_mapping"] = []

        with pytest.raises(ValueError, match="output_mapping must be a dict"):
            SubWorkflow.from_config(config)

    def test_missing_from_node_raises(self, tmp_workspace):
        """Referencing a non-existent node name raises KeyError."""
        config = {
            "name": "bad_ref",
            "inputs": {
                "image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
            },
            "outputs": {"mask": {"type": "Path"}},
            "nodes": [
                {
                    "name": "seg",
                    "tool_class": "StubSegmenter",
                    "tool_module": "tests.integration.conftest",
                    "inputs": {
                        "input_image": {"from_node": "nonexistent", "column": "output"},
                    },
                },
            ],
            "output_mapping": {
                "mask": {"from_node": "seg", "column": "mask"},
            },
        }
        sw = SubWorkflow.from_config(config)
        load = FileLoader()

        with pytest.raises(KeyError):
            with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                sw(image=raw["path"])
