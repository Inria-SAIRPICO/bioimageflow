"""Clean Workflow API contract tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from bioimageflow import Workflow
from bioimageflow.engine import DefaultEngine, SequentialEngine
from bioimageflow.node import get_active_workflow


def test_workflow_defaults_to_direct_parallel_engine() -> None:
    wf = Workflow()

    assert wf.engine_type == "direct"
    assert wf.execution == "parallel"
    assert not hasattr(wf, "use_wetlands")
    assert not hasattr(wf, "max_age")
    assert not hasattr(wf, "max_executions")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"use_wetlands": False}, "use_wetlands"),
        ({"max_age": "7d"}, "max_age"),
        ({"max_executions": 3}, "max_executions"),
    ],
)
def test_workflow_rejects_removed_constructor_arguments(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(TypeError, match=message):
        Workflow(**kwargs)


def test_workflow_builds_direct_parallel_engine() -> None:
    engine = Workflow(engine="direct", execution="parallel")._make_engine()

    assert isinstance(engine, DefaultEngine)
    assert not isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is False
    assert engine._force_sequential is False


def test_workflow_builds_wetlands_sequential_engine() -> None:
    engine = Workflow(engine="wetlands", execution="sequential")._make_engine()

    assert isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is True
    assert engine._force_sequential is True


def test_workflow_rejects_unknown_engine_and_execution() -> None:
    with pytest.raises(ValueError, match="engine"):
        Workflow(engine="parsl")

    with pytest.raises(ValueError, match="execution"):
        Workflow(execution="serial")


def test_workflow_to_dict_uses_clean_config(tmp_path) -> None:
    wf = Workflow(storage_path=tmp_path, engine="direct", execution="sequential")

    config = wf.to_dict()["config"]

    assert config["engine"] == "direct"
    assert config["execution"] == "sequential"
    assert "use_wetlands" not in config
    assert "max_age" not in config
    assert "max_executions" not in config


def test_active_workflow_is_context_local_across_threads() -> None:
    def active_inside_context() -> Workflow:
        with Workflow() as wf:
            assert get_active_workflow() is wf
            return wf

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: active_inside_context(), range(2))

    assert first is not second
    assert get_active_workflow() is None
