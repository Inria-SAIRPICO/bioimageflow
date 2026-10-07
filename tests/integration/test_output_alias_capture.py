"""Direct execution keeps nominal output authority across definition capture."""

from pathlib import Path

import pytest

from bioimageflow import Workflow
from tests.testkit.output_alias_tools import (
    DEFAULT_ALIAS_CALLS,
    DefaultAliasOutputs,
    DefaultGlobalAliasRow,
    GlobalAliasBatch,
    GlobalAliasRow,
    SelfOutputsRow,
    UnrelatedOutputsRow,
)


@pytest.mark.parametrize("tool_type", [GlobalAliasRow, GlobalAliasBatch, SelfOutputsRow],
                         ids=["global-row", "global-batch", "self-outputs"])
def test_nested_compute_accepts_its_declared_output_class(tmp_path, tool_type):
    inner = Workflow(engine="direct", storage_path=tmp_path / "inner")
    with inner:
        source = tool_type()()
    inner.output("value", source["value"])
    inner.output("result", source["result"])
    with Workflow(engine="direct", storage_path=tmp_path / "outer") as outer:
        invocation = inner(name="nested")

    frame = outer.compute(invocation)

    assert frame["value"].tolist() == [7]
    destination = Path(frame.iloc[0]["result"])
    assert destination.name == "alias_0.txt"
    assert destination.read_text() == "7"


def test_compute_refuses_unrelated_same_name_same_shape_output(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        source = UnrelatedOutputsRow()()

    with pytest.raises(TypeError, match="plain dictionary"):
        workflow.compute(source)


def test_nested_compute_refuses_nominal_output_default_drift_before_callback(tmp_path, monkeypatch):
    inner = Workflow(engine="direct", storage_path=tmp_path / "inner")
    with inner:
        source = DefaultGlobalAliasRow()()
    inner.output("value", source["value"])
    inner.output("result", source["result"])
    with Workflow(engine="direct", storage_path=tmp_path / "outer") as outer:
        invocation = inner(name="nested")
    DEFAULT_ALIAS_CALLS.clear()
    monkeypatch.setattr(DefaultAliasOutputs, "value", 8)

    with pytest.raises(ValueError, match="Nominal output declaration changed"):
        outer.compute(invocation)

    assert DEFAULT_ALIAS_CALLS == []
