"""Tests for the GUI validation/planning API (A1–A6 and I1–I4 in plan-gui-validation-api.md)."""

import json
from pathlib import Path
from typing import Annotated, Any

import pytest

from pydantic import Field

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    IOModel,
    ImageSpec,
    ProcessingTool,
    Semantic,
    Layout,
    Template,
)
from bioimageflow import (
    NodePlan,
    SourceToolUpstreamError,
    ValidationError,
    Workflow,
    get_inputs_schema,
    serialize_image_spec,
    serialize_resolved_outputs,
    serialize_tool_metadata,
    topological_order,
    validate_parameters,
)
from bioimageflow.node import BindingError, ColumnNotFoundError, IndexAlignmentError

from .conftest import (
    FileLoader,
    StubSegmenter,
    StubStats,
)


# ---------------------------------------------------------------------------
# A1 — ValidationError dataclass
# ---------------------------------------------------------------------------


class TestValidationErrorDataclass:
    def test_frozen(self) -> None:
        err = ValidationError(kind="missing_input", message="x")
        with pytest.raises(Exception):
            err.message = "mutated"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = ValidationError(kind="cycle", message="m", node="n")
        b = ValidationError(kind="cycle", message="m", node="n")
        c = ValidationError(kind="cycle", message="m", node="other")
        assert a == b
        assert a != c

    def test_default_path_is_tuple(self) -> None:
        err = ValidationError(kind="cycle", message="m")
        assert err.path == ()
        assert isinstance(err.path, tuple)

    def test_binding_error_to_validation_error(self) -> None:
        exc = BindingError("oops")
        err = exc.to_validation_error(node="n1", field="f1")
        assert err.kind == "missing_input"
        assert err.node == "n1"
        assert err.field == "f1"
        assert "oops" in err.message

    def test_column_not_found_to_validation_error(self) -> None:
        exc = ColumnNotFoundError("missing col")
        err = exc.to_validation_error(node="n2", field="f2")
        assert err.kind == "column_not_found"
        assert err.node == "n2"

    def test_index_alignment_to_validation_error(self) -> None:
        exc = IndexAlignmentError("misaligned")
        err = exc.to_validation_error(node="n3")
        assert err.kind == "construction_failed"


# ---------------------------------------------------------------------------
# A2 — Error-collector
# ---------------------------------------------------------------------------


class TestCaptureErrors:
    def test_capture_off_raises_like_today(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            with pytest.raises(BindingError):
                StubSegmenter()()  # missing required input

    def test_capture_captures_multiple_errors_one_pass(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            with wf.capture_errors() as errs:
                load = FileLoader()(path="/tmp/x")
                # 3 distinct problems on separate nodes
                StubSegmenter()(input_image=load["nonexistent"])       # column_not_found
                StubSegmenter()(input_image=load["path"], bogus=1)    # unknown_input
                StubSegmenter()()                                      # missing_input
        kinds = {e.kind for e in errs}
        assert "column_not_found" in kinds
        assert "unknown_input" in kinds
        assert "missing_input" in kinds
        assert len(errs) >= 3

    def test_nested_captures_do_not_share_buffers(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            with wf.capture_errors() as outer:
                with wf.capture_errors() as inner:
                    StubSegmenter()()  # inner captures
                # outer shouldn't see inner's error
                StubSegmenter()()  # outer captures its own
        assert len(inner) == 1
        assert len(outer) == 1
        assert inner[0] != outer[0] or inner[0].node != outer[0].node

    def test_capture_active_no_errors_workflow_still_usable(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            with wf.capture_errors() as errs:
                load = FileLoader()(path=str(tmp_path))
                StubSegmenter()(input_image=load["path"])
        assert errs == []
        # Workflow is usable — we can call plan/validate
        assert wf.validate() == []


# ---------------------------------------------------------------------------
# A3 — from_dict / to_dict
# ---------------------------------------------------------------------------


class TestFromDictToDict:
    def test_round_trip_strict(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"], diameter=25.0)

        data = wf.to_dict()
        wf2 = Workflow.from_dict(data)
        assert isinstance(wf2, Workflow)
        assert set(wf2._nodes) == set(wf._nodes)
        # Constants preserved
        seg2 = next(
            n for n in wf2._nodes.values()
            if type(n.tool).__name__ == "StubSegmenter"
        )
        assert seg2._constant_bindings.get("diameter") == 25.0

    def test_load_and_from_dict_equivalent(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"])

        export_path = tmp_path / "wf.json"
        wf.export(export_path)

        wf_loaded = Workflow.load(export_path)
        wf_from_dict = Workflow.from_dict(json.loads(export_path.read_text()))
        assert isinstance(wf_from_dict, Workflow)
        assert set(wf_loaded._nodes) == set(wf_from_dict._nodes)

    def test_partial_unknown_tool(self, tmp_path: Path) -> None:
        data = {
            "nodes": [
                {
                    "name": "x",
                    "tool_module": "no.such.module",
                    "tool_class": "Foo",
                    "constants": {},
                    "args": [],
                },
            ],
            "edges": [],
            "config": {"storage_path": str(tmp_path)},
        }
        wf, errs = Workflow.from_dict(data, validate_only=True, partial=True)
        assert isinstance(wf, Workflow)
        assert any(e.kind == "unknown_tool" for e in errs)

    def test_partial_three_broken_nodes(self, tmp_path: Path) -> None:
        data = {
            "nodes": [
                {"name": "a", "tool_module": "no.mod.a", "tool_class": "A", "constants": {}, "args": []},
                {"name": "b", "tool_module": "no.mod.b", "tool_class": "B", "constants": {}, "args": []},
                {"name": "c", "tool_module": "no.mod.c", "tool_class": "C", "constants": {}, "args": []},
            ],
            "edges": [],
            "config": {"storage_path": str(tmp_path)},
        }
        wf, errs = Workflow.from_dict(data, validate_only=True, partial=True)
        assert sum(1 for e in errs if e.kind == "unknown_tool") == 3

    def test_edge_referencing_unknown_from_node_partial(self, tmp_path: Path) -> None:
        data = {
            "nodes": [
                {
                    "name": "downstream",
                    "tool_module": "tests.integration.conftest",
                    "tool_class": "StubSegmenter",
                    "constants": {},
                    "args": [],
                },
            ],
            "edges": [
                {"from": "does_not_exist", "to": "downstream",
                 "column": "path", "field": "input_image"},
            ],
            "config": {"storage_path": str(tmp_path)},
        }
        wf, errs = Workflow.from_dict(data, validate_only=True, partial=True)
        assert any(e.kind == "missing_input" for e in errs)

    def test_storage_path_override(self, tmp_path: Path) -> None:
        override = tmp_path / "override"
        data = {
            "nodes": [],
            "edges": [],
            "config": {"storage_path": "/ignored"},
        }
        wf = Workflow.from_dict(data, storage_path_override=override)
        assert isinstance(wf, Workflow)
        assert str(wf.storage_path) == str(override)

    # --- validate_only / partial flag matrix ---

    def _bad_data(self, tmp_path: Path) -> dict:
        return {
            "nodes": [
                {"name": "a", "tool_module": "no.mod.a", "tool_class": "A",
                 "constants": {}, "args": []},
                {"name": "b", "tool_module": "no.mod.b", "tool_class": "B",
                 "constants": {}, "args": []},
            ],
            "edges": [],
            "config": {"storage_path": str(tmp_path)},
        }

    def test_default_strict_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Exception):  # ImportError / ModuleNotFoundError etc.
            Workflow.from_dict(self._bad_data(tmp_path))

    def test_partial_true_validate_only_false_raises_aggregated(
        self, tmp_path: Path,
    ) -> None:
        # partial collects errors, validate_only=False raises a summary.
        with pytest.raises(ValueError, match="construction failed"):
            Workflow.from_dict(self._bad_data(tmp_path), partial=True)

    def test_validate_only_true_partial_false_returns_first_error(
        self, tmp_path: Path,
    ) -> None:
        # Fail-fast tuple mode: capture the first failure, return tuple.
        wf, errs = Workflow.from_dict(
            self._bad_data(tmp_path), validate_only=True, partial=False,
        )
        assert isinstance(wf, Workflow)
        assert len(errs) == 1  # stopped at first failure

    def test_validate_only_true_partial_true_returns_all(
        self, tmp_path: Path,
    ) -> None:
        wf, errs = Workflow.from_dict(
            self._bad_data(tmp_path), validate_only=True, partial=True,
        )
        assert isinstance(wf, Workflow)
        assert sum(1 for e in errs if e.kind == "unknown_tool") == 2

    def test_collect_errors_kwarg_is_removed(self, tmp_path: Path) -> None:
        # The removed `collect_errors=` kwarg must
        # raise TypeError.
        with pytest.raises(TypeError):
            Workflow.from_dict(self._bad_data(tmp_path), collect_errors=True)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# A4 — Workflow.validate
# ---------------------------------------------------------------------------


class _BadConstraintTool(ProcessingTool):
    """Inputs has a gt=0 constraint that can surface as parameter_invalid."""

    display_name = "BadConstraint"
    environment = EnvironmentSpec(
        name="_validateenv",
        dependencies={"conda": ["numpy"], "python": "3.12"},
    )

    class Inputs(IOModel):
        diameter: Annotated[float, Field(gt=0)] = 1.0

    class Outputs(IOModel):
        result: Path = Template("{diameter}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        p = Path(arguments.result)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return self.Outputs(result=p)


class TestValidate:
    def test_empty_workflow(self) -> None:
        wf = Workflow(engine="direct")
        assert wf.validate() == []

    def test_valid_workflow(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            seg = StubSegmenter()(input_image=load["path"])
            StubStats()(image=load["path"], mask=seg["mask"])
        assert wf.validate() == []

    def test_missing_required_after_capture(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            with wf.capture_errors():
                StubSegmenter()()
        errs = wf.validate()
        assert any(e.kind == "missing_input" and e.field == "input_image" for e in errs)

    def test_parameter_invalid_via_pydantic_constraint(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            _BadConstraintTool()(diameter=-1)  # gt=0 violated
        errs = wf.validate()
        assert any(e.kind == "parameter_invalid" and e.field == "diameter" for e in errs)

    def test_validate_parameters_standalone(self) -> None:
        errs = validate_parameters(_BadConstraintTool, {"diameter": -5})
        assert errs
        assert errs[0].kind == "parameter_invalid"

    def test_validate_parameters_empty(self) -> None:
        errs = validate_parameters(_BadConstraintTool, {})
        assert errs == []

    def test_topological_order_raises_on_cycle(self) -> None:
        # A cycle is only constructible by mutating upstream_nodes after the fact.
        wf = Workflow(engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
        # Force a cycle.
        seg._upstream_nodes.add(seg)
        from graphlib import CycleError
        with pytest.raises(CycleError):
            topological_order(wf)
        errs = wf.validate()
        assert any(e.kind == "cycle" for e in errs)

    def test_plan_raises_cycle_in_workflow_error(self) -> None:
        from bioimageflow import CycleInWorkflowError

        wf = Workflow(engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
        seg._upstream_nodes.add(seg)
        with pytest.raises(CycleInWorkflowError) as excinfo:
            wf.plan()
        # Carries the offending node names; subclass of ValueError.
        assert isinstance(excinfo.value, ValueError)
        assert excinfo.value.nodes  # non-empty
        # validate() still reports cycles non-fatally (unchanged behavior).
        errs = wf.validate()
        assert any(e.kind == "cycle" for e in errs)


# ---------------------------------------------------------------------------
# A5 — plan()
# ---------------------------------------------------------------------------


class TestPlan:
    def test_source_only_plan(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            FileLoader()(path=str(tmp_path))
        plan = wf.plan()
        assert "FileLoader_1" in plan
        entry = plan["FileLoader_1"]
        assert isinstance(entry, NodePlan)
        assert entry.logical_signature != ""
        assert entry.cached is False  # no cache yet
        assert entry.skipped is False

    def test_plan_parity_with_compute(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")
        (src / "b.txt").write_text("b")

        def build() -> Workflow:
            wf = Workflow(engine="direct", storage_path=tmp_path / "cache")
            with wf:
                load = FileLoader()(path=str(src))
                StubSegmenter()(input_image=load["path"], diameter=20.0)
            return wf

        # Plan before compute
        wf1 = build()
        plan_pre = wf1.plan()
        assert all(not p.cached for p in plan_pre.values())

        # Compute
        wf1.compute()

        # Fresh workflow, fresh plan
        wf2 = build()
        plan_post = wf2.plan()
        # Source-node diagnostic identity is stable. Downstream diagnostics may
        # change after compute because final result keys include selected
        # upstream record IDs once they exist.
        assert plan_pre["FileLoader_1"].logical_signature == plan_post["FileLoader_1"].logical_signature
        # All cached
        assert all(p.cached for p in plan_post.values())

    def test_plan_parity_dev_mode(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        wf = Workflow(engine="direct", storage_path=tmp_path / "cache")
        with wf:
            load = FileLoader()(path=str(src))
            StubSegmenter()(input_image=load["path"])
        plan_dev = wf.plan(dev_mode=True)
        plan_nodev = wf.plan(dev_mode=False)
        # dev_mode should produce different hashes (source_hash included)
        for name in plan_dev:
            assert plan_dev[name].logical_signature != plan_nodev[name].logical_signature

    def test_plan_disabled_node(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            seg = StubSegmenter()(input_image=load["path"])
        seg.disable()
        plan = wf.plan()
        assert plan["StubSegmenter_1"].skipped is True

    def test_plan_does_not_launch_wetlands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bioimageflow.env_manager as em

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Wetlands must not launch during plan()")

        monkeypatch.setattr(em.WetlandsEnvManager, "__init__", _boom)

        wf = Workflow(storage_path=tmp_path, engine="wetlands")
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"])
        plan = wf.plan()
        assert plan  # ran successfully


# ---------------------------------------------------------------------------
# A6 — introspection helpers
# ---------------------------------------------------------------------------


class TestIntrospectionHelpers:
    def test_topological_order_method(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
            StubStats()(image=load["path"], mask=seg["mask"])
        order = wf.topological_order()
        assert order.index("FileLoader_1") < order.index("StubSegmenter_1")
        assert order.index("StubSegmenter_1") < order.index("StubStats_1")

    def test_downstream_of(self) -> None:
        wf = Workflow(engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
            StubStats()(image=load["path"], mask=seg["mask"])
        assert wf.downstream_of("FileLoader_1") == {"StubSegmenter_1", "StubStats_1"}
        assert wf.downstream_of("StubStats_1") == set()

    def test_downstream_of_unknown(self) -> None:
        wf = Workflow(engine="direct")
        with pytest.raises(KeyError):
            wf.downstream_of("nope")

    def test_serialize_image_spec_shape(self) -> None:
        spec = ImageSpec(
            semantics={Semantic.INTENSITY, Semantic.LABEL},
            layouts={Layout.PLANAR},
            dtypes={"uint8"},
            formats={"tif"},
        )
        out = serialize_image_spec(spec)
        assert out == {
            "semantics": ["intensity", "label"],
            "layouts": ["YX"],
            "dtypes": ["uint8"],
            "formats": ["tif"],
        }

    def test_serialize_image_spec_none(self) -> None:
        assert serialize_image_spec(None) is None

    def test_get_inputs_schema_has_serialized_key(self) -> None:
        schema = get_inputs_schema(StubSegmenter())
        entry = schema["input_image"]
        assert "image_spec_serialized" in entry
        assert entry["image_spec_serialized"]["semantics"] == ["intensity"]

    def test_serialize_tool_metadata_files(self) -> None:
        """Wire-format parity test for the platform-consumed tool metadata."""
        from bioimageflow_common_tools import Files

        meta = serialize_tool_metadata(Files)
        assert meta["tool_type"] == "DataFrameTool"
        assert meta["accepts_upstream"] is False
        assert meta["dataframe_output"] is True
        json.dumps(meta)  # JSON-safe

    def test_serialize_tool_metadata_processing_tool(self) -> None:
        meta = serialize_tool_metadata(StubSegmenter)
        assert meta["tool_type"] == "ProcessingTool"
        assert meta["accepts_upstream"] is True
        assert meta["dataframe_output"] is True

    def test_serialize_tool_metadata_merge_tool(self) -> None:
        from bioimageflow_common_tools import CrossJoin

        meta = serialize_tool_metadata(CrossJoin)
        assert meta["tool_type"] == "DataFrameTool"
        assert meta["accepts_upstream"] is True
        # Merge tool — resolved schema depends on upstreams, so the GUI
        # must call serialize_resolved_outputs to render per-column pins.
        assert meta["dynamic_outputs"] is True


# ---------------------------------------------------------------------------
# Source-tool enforcement (accepts_upstream)
# ---------------------------------------------------------------------------


class TestSourceToolUpstream:
    def test_source_tool_with_upstream_raises(self, tmp_path: Path) -> None:
        from bioimageflow_common_tools import Files

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            other = Files()(path=str(tmp_path))
            with pytest.raises(SourceToolUpstreamError):
                Files()(other, path=str(tmp_path))

    def test_source_tool_kwargs_only_works(self, tmp_path: Path) -> None:
        from bioimageflow_common_tools import Files

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            Files()(path=str(tmp_path))
        # No exception → the workflow built fine.
        assert "Files_1" in wf._nodes


# ---------------------------------------------------------------------------
# Dynamic output schema (resolve_outputs / serialize_resolved_outputs)
# ---------------------------------------------------------------------------


class TestSerializeResolvedOutputsWireFormat:
    """Parity tests for the wire-format the platform consumes for resolved
    output pins on configured nodes.
    """

    def test_unconfigured_generate_has_no_columns(self, tmp_path: Path) -> None:
        # Generate without column_name can't even be constructed (required
        # field). Cover the unresolved path with a custom DataFrameTool below.
        from bioimageflow.dataframe_tool import DataFrameTool

        class Dyn(DataFrameTool):
            display_name = "Dyn"

            class Inputs(IOModel):
                pass

            @classmethod
            def resolve_outputs(cls, inputs=None):
                return None

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            n = Dyn()()
            out = serialize_resolved_outputs(n)
            assert out == {"resolved": False, "columns": {}}
            json.dumps(out)

    def test_generate_resolved_after_column_name(self, tmp_path: Path) -> None:
        from bioimageflow_common_tools import Generate

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            g = Generate()(column_name="sensitivity", values=[1, 2, 3])
            out = serialize_resolved_outputs(g)
            assert out["resolved"] is True
            assert set(out["columns"].keys()) == {"sensitivity"}
            json.dumps(out)

    def test_cross_join_resolved_schema_for_parameter_space(self, tmp_path: Path) -> None:
        """parameter_space_exploration's exact wiring resolves at construction."""
        from bioimageflow_common_tools import CrossJoin, Files, Generate

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            files = Files()(path=str(tmp_path))
            sens = Generate()(column_name="sensitivity", values=[0.1, 0.2])
            size = Generate()(column_name="size", values=[1, 2])
            grid = CrossJoin()(files, sens, size)
            out = serialize_resolved_outputs(grid)
            assert out["resolved"] is True
            assert set(out["columns"].keys()) == {"path", "sensitivity", "size"}
            json.dumps(out)


# ---------------------------------------------------------------------------
# I1–I4 — integration tests
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_I1_full_round_trip(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"], diameter=15.0)

        # export → load
        p = tmp_path / "wf.json"
        wf.export(p)
        wf_loaded = Workflow.load(p)

        # to_dict → from_dict(validate_only=True, partial=True)
        data = wf_loaded.to_dict()
        wf_collect, errs = Workflow.from_dict(data, validate_only=True, partial=True)
        assert errs == []
        assert isinstance(wf_collect, Workflow)
        assert set(wf.to_dict()["nodes"][0].keys()) == \
            set(wf_collect.to_dict()["nodes"][0].keys())

    def test_I2_broken_graph_survey(self, tmp_path: Path) -> None:
        # Build a dict with: unknown_tool + missing edge + bad constant + type-mismatch
        data = {
            "nodes": [
                {
                    "name": "load",
                    "tool_module": "tests.integration.conftest",
                    "tool_class": "FileLoader",
                    "constants": {"path": {"__type__": "str", "value": str(tmp_path)}},
                    "args": [],
                },
                {
                    "name": "seg",
                    "tool_module": "tests.integration.conftest",
                    "tool_class": "StubSegmenter",
                    # diameter will be validated by Pydantic in validate() — but conftest's
                    # StubSegmenter has no gt=0 constraint, so use an unusual-type value.
                    "constants": {"diameter": {"__type__": "str", "value": "not-a-number"}},
                    "args": [],
                },
                {
                    "name": "missing_tool_node",
                    "tool_module": "no.such.module",
                    "tool_class": "NoSuchClass",
                    "constants": {},
                    "args": [],
                },
            ],
            "edges": [
                {"from": "load", "to": "seg",
                 "column": "path", "field": "input_image"},
            ],
            "config": {"storage_path": str(tmp_path)},
        }
        wf, errs = Workflow.from_dict(data, validate_only=True, partial=True)
        assert any(e.kind == "unknown_tool" for e in errs)

        # validate() now runs on the partial wf
        v_errs = wf.validate()
        v_kinds = {e.kind for e in v_errs}
        assert "parameter_invalid" in v_kinds

    def test_I3_plan_parity_under_dev_mode(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        def build(diameter: float) -> Workflow:
            wf = Workflow(engine="direct", storage_path=tmp_path / "cache")
            with wf:
                load = FileLoader()(path=str(src))
                StubSegmenter()(input_image=load["path"], diameter=diameter)
            return wf

        wf1 = build(20.0)
        p_before = wf1.plan()
        wf1.compute()
        p_after = build(20.0).plan()
        for name, entry in p_after.items():
            assert entry.cached is True
        assert p_after["FileLoader_1"].logical_signature == p_before["FileLoader_1"].logical_signature

        # Change the constant → the changed node & its descendants cache-miss
        wf2 = build(99.0)
        p_changed = wf2.plan()
        assert p_changed["StubSegmenter_1"].cached is False
        # FileLoader is upstream, not affected
        assert p_changed["FileLoader_1"].logical_signature == p_after["FileLoader_1"].logical_signature

    def test_I4_no_wetlands_under_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bioimageflow.env_manager as em

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("plan() launched Wetlands")

        monkeypatch.setattr(em.WetlandsEnvManager, "__init__", _boom)

        wf = Workflow(storage_path=tmp_path, engine="wetlands")
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"])
        # plan() must succeed without hitting Wetlands
        plan = wf.plan()
        assert plan
