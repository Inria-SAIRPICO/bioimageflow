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
        assert (
            plan_pre["FileLoader_1"].logical_signature
            == plan_post["FileLoader_1"].logical_signature
        )
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
            assert (
                plan_dev[name].logical_signature != plan_nodev[name].logical_signature
            )

    def test_plan_disabled_node(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            seg = StubSegmenter()(input_image=load["path"])
        seg.disable()
        plan = wf.plan()
        assert plan["StubSegmenter_1"].skipped is True

    def test_plan_does_not_launch_wetlands(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
