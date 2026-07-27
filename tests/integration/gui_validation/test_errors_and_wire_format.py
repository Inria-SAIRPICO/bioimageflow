"""Focused tests split from ``tests/integration/test_gui_validation_api.py``."""

# ruff: noqa: F401

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

from tests.testkit.integration_tools import (
    FileLoader,
    StubSegmenter,
    StubStats,
)


from tests.testkit.gui_validation import (
    _graph,
    _tool_node,
)


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


class TestCaptureErrors:
    def test_capture_off_raises_like_today(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            with pytest.raises(BindingError):
                StubSegmenter()()  # missing required input

    def test_capture_captures_multiple_errors_one_pass(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            with wf.capture_errors() as errs:
                load = FileLoader()(path="/tmp/x")
                # 3 distinct problems on separate nodes
                StubSegmenter()(input_image=load["nonexistent"])  # column_not_found
                StubSegmenter()(input_image=load["path"], bogus=1)  # unknown_input
                StubSegmenter()()  # missing_input
        kinds = {e.kind for e in errs}
        assert "column_not_found" in kinds
        assert "unknown_input" in kinds
        assert "missing_input" in kinds
        assert len(errs) >= 3

    def test_nested_captures_do_not_share_buffers(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            with wf.capture_errors() as outer:
                with wf.capture_errors() as inner:
                    StubSegmenter()()  # inner captures
                # outer shouldn't see inner's error
                StubSegmenter()()  # outer captures its own
        assert len(inner) == 1
        assert len(outer) == 1
        assert inner[0] != outer[0] or inner[0].node != outer[0].node

    def test_capture_active_no_errors_workflow_still_usable(
        self, tmp_path: Path
    ) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            with wf.capture_errors() as errs:
                load = FileLoader()(path=str(tmp_path))
                StubSegmenter()(input_image=load["path"])
        assert errs == []
        # Workflow is usable — we can call plan/validate
        assert wf.validate() == []


class TestFromDictToDict:
    @pytest.mark.compat
    def test_round_trip_strict(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"], diameter=25.0)

        data = wf.to_dict()
        wf2 = Workflow.from_dict(data, storage_path=tmp_path)
        assert isinstance(wf2, Workflow)
        assert set(wf2._nodes) == set(wf._nodes)
        # Constants preserved
        seg2 = next(
            n for n in wf2._nodes.values() if type(n.tool).__name__ == "StubSegmenter"
        )
        assert seg2._constant_bindings.get("diameter") == 25.0

    def test_load_and_from_dict_equivalent(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"])

        export_path = tmp_path / "wf.json"
        wf.export(export_path)

        wf_loaded = Workflow.load(export_path, storage_path=tmp_path)
        wf_from_dict = Workflow.from_dict(
            json.loads(export_path.read_text()),
            storage_path=tmp_path,
        )
        assert isinstance(wf_from_dict, Workflow)
        assert set(wf_loaded._nodes) == set(wf_from_dict._nodes)

    def test_partial_unknown_tool(self, tmp_path: Path) -> None:
        data = _graph(nodes=[_tool_node("x", "no.such.module", "Foo")])
        wf, errs = Workflow.from_dict(
            data,
            storage_path=tmp_path,
            validate_only=True,
            partial=True,
        )
        assert isinstance(wf, Workflow)
        assert any(e.kind == "unknown_tool" for e in errs)

    def test_partial_three_broken_nodes(self, tmp_path: Path) -> None:
        data = _graph(
            nodes=[
                _tool_node("a", "no.mod.a", "A"),
                _tool_node("b", "no.mod.b", "B"),
                _tool_node("c", "no.mod.c", "C"),
            ],
        )
        wf, errs = Workflow.from_dict(
            data,
            storage_path=tmp_path,
            validate_only=True,
            partial=True,
        )
        assert sum(1 for e in errs if e.kind == "unknown_tool") == 3

    def test_edge_referencing_unknown_from_node_partial(self, tmp_path: Path) -> None:
        data = _graph(
            nodes=[
                _tool_node(
                    "downstream",
                    "tests.testkit.integration_tools",
                    "StubSegmenter",
                )
            ],
            edges=[
                {
                    "type": "column",
                    "id": "missing-source",
                    "source_node": "does_not_exist",
                    "source_output": "path",
                    "target_node": "downstream",
                    "target_input": "input_image",
                }
            ],
        )
        wf, errs = Workflow.from_dict(
            data,
            storage_path=tmp_path,
            validate_only=True,
            partial=True,
        )
        assert any(e.kind == "missing_input" for e in errs)

    def test_storage_path_is_runtime_only(self, tmp_path: Path) -> None:
        runtime_storage = tmp_path / "runtime"
        data = _graph()
        wf = Workflow.from_dict(data, storage_path=runtime_storage)
        assert isinstance(wf, Workflow)
        assert wf.storage_path == runtime_storage.resolve()
        assert "storage_path" not in wf.to_dict()["config"]

    def test_serialized_storage_path_is_rejected(self, tmp_path: Path) -> None:
        data = _graph()
        data["config"]["storage_path"] = "./stale"

        with pytest.raises(ValueError, match="Unknown workflow config field"):
            Workflow.from_dict(data, storage_path=tmp_path)

    # --- validate_only / partial flag matrix ---

    def _bad_data(self, tmp_path: Path) -> dict:
        return _graph(
            nodes=[
                _tool_node("a", "no.mod.a", "A"),
                _tool_node("b", "no.mod.b", "B"),
            ],
        )

    def test_default_strict_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Exception):  # ImportError / ModuleNotFoundError etc.
            Workflow.from_dict(self._bad_data(tmp_path), storage_path=tmp_path)

    def test_partial_true_validate_only_false_raises_aggregated(
        self,
        tmp_path: Path,
    ) -> None:
        # partial collects errors, validate_only=False raises a summary.
        with pytest.raises(ValueError, match="construction failed"):
            Workflow.from_dict(
                self._bad_data(tmp_path),
                storage_path=tmp_path,
                partial=True,
            )

    def test_validate_only_true_partial_false_returns_first_error(
        self,
        tmp_path: Path,
    ) -> None:
        # Fail-fast tuple mode: capture the first failure, return tuple.
        wf, errs = Workflow.from_dict(
            self._bad_data(tmp_path),
            storage_path=tmp_path,
            validate_only=True,
            partial=False,
        )
        assert isinstance(wf, Workflow)
        assert len(errs) == 1  # stopped at first failure

    def test_validate_only_true_partial_true_returns_all(
        self,
        tmp_path: Path,
    ) -> None:
        wf, errs = Workflow.from_dict(
            self._bad_data(tmp_path),
            storage_path=tmp_path,
            validate_only=True,
            partial=True,
        )
        assert isinstance(wf, Workflow)
        assert sum(1 for e in errs if e.kind == "unknown_tool") == 2
