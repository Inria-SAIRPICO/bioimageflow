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


class TestIntegration:
    def test_I1_full_round_trip(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"], diameter=15.0)

        # export → load
        p = tmp_path / "wf.json"
        wf.export(p)
        wf_loaded = Workflow.load(p, storage_path=tmp_path / "results")

        # to_dict → from_dict(validate_only=True, partial=True)
        data = wf_loaded.to_dict()
        wf_collect, errs = Workflow.from_dict(
            data,
            storage_path=tmp_path / "results",
            validate_only=True,
            partial=True,
        )
        assert errs == []
        assert isinstance(wf_collect, Workflow)
        assert set(wf.to_dict()["nodes"][0].keys()) == set(
            wf_collect.to_dict()["nodes"][0].keys()
        )

    def test_I2_broken_graph_survey(self, tmp_path: Path) -> None:
        # Build a dict with: unknown_tool + missing edge + bad constant + type-mismatch
        data = _graph(
            nodes=[
                _tool_node(
                    "load",
                    "tests.testkit.integration_tools",
                    "FileLoader",
                    constants={"path": {"__type__": "str", "value": str(tmp_path)}},
                ),
                _tool_node(
                    "seg",
                    "tests.testkit.integration_tools",
                    "StubSegmenter",
                    constants={
                        "diameter": {"__type__": "str", "value": "not-a-number"}
                    },
                ),
                _tool_node("missing_tool_node", "no.such.module", "NoSuchClass"),
            ],
            edges=[
                {
                    "type": "column",
                    "id": "load-seg",
                    "source_node": "load",
                    "source_output": "path",
                    "target_node": "seg",
                    "target_input": "input_image",
                }
            ],
        )
        wf, errs = Workflow.from_dict(
            data,
            storage_path=tmp_path / "results",
            validate_only=True,
            partial=True,
        )
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
        assert (
            p_after["FileLoader_1"].logical_signature
            == p_before["FileLoader_1"].logical_signature
        )

        # Change the constant → the changed node & its descendants cache-miss
        wf2 = build(99.0)
        p_changed = wf2.plan()
        assert p_changed["StubSegmenter_1"].cached is False
        # FileLoader is upstream, not affected
        assert (
            p_changed["FileLoader_1"].logical_signature
            == p_after["FileLoader_1"].logical_signature
        )

    def test_I4_no_wetlands_under_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
