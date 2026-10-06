"""Explicit DataFrame merges receive the actual positional provider tables."""

import pandas as pd

from bioimageflow import DataFrameTool, Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage
from bioimageflow_common_tools import Concat, CrossJoin, Generate


class RelatedChildren(DataFrameTool):
    def transform(self, df, arguments):
        selected = df.loc[df["parent"] == "A"]
        assert list(selected.index) == ["0"]
        return pd.DataFrame(
            {"child": ["I", "J"]},
            index=[str(selected.index[0]) + "::0", str(selected.index[0]) + "::1"],
        )


class LiteralInner(DataFrameTool):
    pass


def _operands():
    parent = Generate()(column_name="parent", values=["A", "B"])
    return parent, RelatedChildren()(parent)


def test_explicit_crossjoin_preserves_independent_pairs_and_warm_record(tmp_path):
    storage_path = tmp_path / "results"
    with Workflow(engine="direct", storage_path=storage_path) as workflow:
        parent, child = _operands()
        merged = CrossJoin()(parent, child, name="cross")
        cold, warm = WorkflowExecutionContext(), WorkflowExecutionContext()
        first = workflow.compute(merged, run_context=cold)
        second = workflow.compute(merged, run_context=warm)

    expected = [
        {"parent": "A", "child": "I"}, {"parent": "A", "child": "J"},
        {"parent": "B", "child": "I"}, {"parent": "B", "child": "J"},
    ]
    assert first.to_dict("records") == expected
    assert second.to_dict("records") == expected
    storage = Storage(storage_path)
    first_record = storage.read_run_node_result(cold.run_id, "cross")
    second_record = storage.read_run_node_result(warm.run_id, "cross")
    assert not first_record.cache_hit and second_record.cache_hit
    assert (first_record.result_key, first_record.record_id) == (second_record.result_key, second_record.record_id)


def test_explicit_empty_operand_has_merge_defined_cardinality(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        parent = Generate()(column_name="parent", values=["A", "B"])
        empty = Generate()(column_name="child", values=[])
        product = workflow.compute(CrossJoin()(parent, empty))
        concatenated = workflow.compute(Concat()(parent, empty))

    assert product.empty and list(product.columns) == ["parent", "child"]
    assert concatenated["parent"].tolist() == ["A", "B"]
    assert concatenated["child"].isna().all()


def test_default_inner_join_uses_literal_coarse_and_fine_indexes(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        parent, child = _operands()
        result = workflow.compute(LiteralInner()(parent, child))

    assert result.empty
    assert list(result.columns) == ["parent", "child"]


def test_concat_preserves_repeated_positional_operand_values_and_order(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        parent, child = _operands()
        result = workflow.compute(Concat()(parent, child, parent))

    assert result.fillna("missing").to_dict("records") == [
        {"parent": "A", "child": "missing"}, {"parent": "B", "child": "missing"},
        {"parent": "missing", "child": "I"}, {"parent": "missing", "child": "J"},
        {"parent": "A", "child": "missing"}, {"parent": "B", "child": "missing"},
    ]
