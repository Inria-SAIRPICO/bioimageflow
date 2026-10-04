"""Collective groups consume the actual batch, independently of output rows."""

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, IndexAlignmentError, Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage
from bioimageflow_common_tools import Collect, CrossJoin
from bioimageflow_core import Arguments, GENERAL_ENV, IOModel, ProcessingTool, RowConsumption, Template


class OrderedValues(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        values: list[int]

    class Outputs(IOModel):
        value: int

    def transform(self, df: Any, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame(
            {"value": arguments.values},
            index=pd.Index([f"observation-{position}" for position in range(len(arguments.values))]),
        )


class SumBatch(ProcessingTool):
    row_consumption = RowConsumption.COLLECTIVE
    environment = GENERAL_ENV

    class Inputs(IOModel):
        value: int
        offset: int = 5

    class Outputs(IOModel):
        total: int
        consumed_count: int

    def process_batch(self, arguments_list: list[Arguments], *, context: Any = None) -> Any:
        if arguments_list:
            offset = arguments_list[0].offset
        else:
            assert context is not None
            assert context.batch_arguments is not None
            offset = context.batch_arguments.offset
        return [self.Outputs(total=sum(row.value for row in arguments_list) + offset,
                             consumed_count=len(arguments_list))]


class EmptyArtifactBatch(SumBatch):
    row_consumption = RowConsumption.COLLECTIVE

    class Outputs(IOModel):
        total: int
        consumed_count: int
        artifact: Path = Template("empty-aggregate.txt")

    def process_batch(self, arguments_list: list[Arguments], *, context: Any = None) -> Any:
        assert arguments_list == [], "a collective empty batch has no fabricated observation"
        assert context is not None and context.batch_arguments is not None
        batch = context.batch_arguments
        artifact = Path(batch.artifact)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"offset={batch.offset};observations=0")
        return [self.Outputs(total=batch.offset, consumed_count=0, artifact=artifact)]


def test_collective_aggregate_is_one_output_for_all_actual_rows(tmp_path: Path) -> None:
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = OrderedValues()(values=[1, 2, 3], name="observations")
        aggregate = SumBatch()(value=source["value"], offset=7, name="aggregate")
        result = workflow.compute(aggregate, run_context=context)
    assert result["total"].tolist() == [13]
    assert result["consumed_count"].tolist() == [3]
    assert len(result) == 1
    outcome = next(outcome for outcome in context.execution_outcomes if outcome.node_key == "aggregate")
    assert outcome.result_key is not None and outcome.record_id is not None
    relation = Storage(tmp_path / "results").load_record_manifest(outcome.result_key, outcome.record_id).row_relation
    assert relation["row_consumption"] == "collective"
    assert relation["domain_kind"] == "aggregate"
    assert relation["groups"] == [{
        "consumed_rows": [{"position": position, "row_index": f"observation-{position}"} for position in range(3)],
        "output_indices": result.index.tolist(),
    }]
    assert all(index not in {"observation-0", "observation-1", "observation-2"} for index in result.index)


def test_collective_empty_batch_keeps_constants_and_output_path(tmp_path: Path) -> None:
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = OrderedValues()(values=[], name="observations")
        aggregate = EmptyArtifactBatch()(value=source["value"], offset=7, name="aggregate")
        result = workflow.compute(aggregate, run_context=context)
    assert result["total"].tolist() == [7]
    assert result["consumed_count"].tolist() == [0]
    assert len(result) == 1
    assert Path(result.iloc[0]["artifact"]).read_text() == "offset=7;observations=0"
    outcome = next(outcome for outcome in context.execution_outcomes if outcome.node_key == "aggregate")
    assert outcome.result_key is not None and outcome.record_id is not None
    relation = Storage(tmp_path / "results").load_record_manifest(outcome.result_key, outcome.record_id).row_relation
    assert relation["groups"] == [{"consumed_rows": [], "output_indices": result.index.tolist()}]


class PredictWithModel(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = GENERAL_ENV

    class Inputs(IOModel):
        value: int
        total: int

    class Outputs(IOModel):
        prediction: int
        total: int

    def process_row(self, arguments: Arguments) -> Any:
        return self.Outputs(prediction=arguments.value + arguments.total, total=arguments.total)


@pytest.mark.parametrize("predict_values, expected", [([1, 2, 3], [7, 8, 9]), ([10, 20], [16, 26])])
def test_collective_model_uses_explicit_cross_join_for_prediction(
    tmp_path: Path, predict_values: list[int], expected: list[int]
) -> None:
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        training = OrderedValues()(values=[1, 2, 3], name="training")
        model = SumBatch()(value=training["value"], offset=0, name="model")
        prediction = OrderedValues()(values=predict_values, name="prediction")
        joined = CrossJoin()(prediction, model, name="explicit-model-inputs")
        output = PredictWithModel()(value=joined["value"], total=joined["total"])
        result = workflow.compute(output)
    assert result["prediction"].tolist() == expected
    assert result["total"].tolist() == [6] * len(predict_values)


def test_collective_model_is_not_implicitly_broadcast_to_observations(tmp_path: Path) -> None:
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = OrderedValues()(values=[1, 2, 3], name="training")
        model = SumBatch()(value=source["value"], offset=0, name="model")
        output = PredictWithModel()(value=source["value"], total=model["total"])
        with pytest.raises(IndexAlignmentError, match="explicit merge"):
            workflow.compute(output)


class MixedNumericValues(OrderedValues):
    class Outputs(IOModel):
        value: int
        weight: float

    def transform(self, df: Any, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame({"value": arguments.values, "weight": [0.25] * len(arguments.values)},
                            index=pd.Index([f"observation-{position}" for position in range(len(arguments.values))]))


class ExpandRows(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = GENERAL_ENV

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        copy_number: int

    def process_row(self, arguments: Arguments) -> Any:
        return [self.Outputs(copy_number=0), self.Outputs(copy_number=1)]


def test_parent_expansion_preserves_integer_dtype_and_order(tmp_path: Path) -> None:
    values = [2**62 + 1, 2**62 + 3]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = MixedNumericValues()(values=values)
        expanded = ExpandRows()(value=source["value"])
        collected = Collect()(source, expanded)
        result = workflow.compute(collected)
    assert result["value"].tolist() == [values[0], values[0], values[1], values[1]]
    assert result["value"].dtype == "int64"
    assert result["weight"].tolist() == [0.25] * 4
    assert result["copy_number"].tolist() == [0, 1, 0, 1]


def test_dataframe_relation_mismatch_refuses_before_staging(tmp_path: Path) -> None:
    from bioimageflow.cache import dataframe_publish
    from bioimageflow.row_relation import ResultRelation, RowAssociation

    storage_path = tmp_path / "uncreated-storage"
    relation = ResultRelation("dataframe", "merge::explicit", "merge", (RowAssociation((), ("wrong",)),))
    with pytest.raises(ValueError, match="output indices"):
        dataframe_publish(storage_path, "joined", "signature", pd.DataFrame({"value": [4]}, index=["actual"]),
                          run_id="run_" + "0" * 32, engine="direct", tool_identity="test", row_relation=relation.to_dict())
    assert not storage_path.exists()
