"""Public cache keys own actual finite field values, not JSON approximations."""

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow, WorkflowExecutionContext
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption


class SeedValue(DataFrameTool):
    environment = GENERAL_ENV
    accepts_upstream = False

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: int

    def transform(self, dataframe, arguments):
        return pd.DataFrame({"value": [4]}, index=["seed"])


class SourceContainerKind(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0

    class Inputs(IOModel):
        payload: list[int] | tuple[int, ...] = [4]

    class Outputs(IOModel):
        observed_kind: str
        value: int

    def process_row(self, arguments):
        type(self).executions += 1
        return self.Outputs(observed_kind=type(arguments.payload).__name__, value=arguments.payload[0])


class BoundContainerKind(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0

    class Inputs(IOModel):
        value: int
        payload: list[int] | tuple[int, ...]

    class Outputs(IOModel):
        observed_kind: str
        value: int

    def process_row(self, arguments):
        type(self).executions += 1
        return self.Outputs(observed_kind=type(arguments.payload).__name__, value=arguments.value + arguments.payload[0])


class DictionaryKeyKind(DataFrameTool):
    environment = GENERAL_ENV
    executions = 0

    class Inputs(IOModel):
        payload: dict

    class Outputs(IOModel):
        observed_kind: str
        value: int

    def transform(self, dataframe, arguments):
        type(self).executions += 1
        key = next(iter(arguments.payload))
        return pd.DataFrame({"observed_kind": [type(key).__name__], "value": [4]}, index=dataframe.index)


class BoundDefaultValues(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0

    class Inputs(IOModel):
        value: int = 999
        payload: list[int] | tuple[int, ...] = [4]

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments):
        type(self).executions += 1
        return self.Outputs(value=arguments.value + arguments.payload[0])


def _outcome(context):
    return next(item for item in context.execution_outcomes if item.node_key == "subject")


def test_bound_defaults_compute_plan_and_equal_explicit_values_share_selection(tmp_path):
    from bioimageflow import NodePlanStatus

    BoundDefaultValues.executions = 0
    owners = []
    outcomes = []
    try:
        for parameters, expected in [({}, 8), ({"payload": [4]}, 8),
                                     ({"payload": [9]}, 13), ({"payload": (4,)}, 8)]:
            with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
                seed = SeedValue()(name="seed")
                subject = BoundDefaultValues()(value=seed["value"], name="subject", **parameters)
            owners.append(workflow.shared_memory_context)
            context = WorkflowExecutionContext()
            assert workflow.compute(subject, run_context=context)["value"].tolist() == [expected]
            outcome = _outcome(context)
            plan = workflow.plan()["subject"]
            assert plan.status is NodePlanStatus.CACHED
            assert (plan.final_result_key, plan.selected_record_id) == (outcome.result_key, outcome.record_id)
            outcomes.append(outcome)
        assert BoundDefaultValues.executions == 3
        assert (outcomes[0].result_key, outcomes[0].record_id) == (outcomes[1].result_key, outcomes[1].record_id)
        assert len({item.result_key for item in (outcomes[0], outcomes[2], outcomes[3])}) == 3

        BoundDefaultValues.Inputs.payload = [9]
        with Workflow(engine="direct", storage_path=tmp_path / "results") as changed:
            seed = SeedValue()(name="seed")
            subject = BoundDefaultValues()(value=seed["value"], name="subject")
        owners.append(changed.shared_memory_context)
        context = WorkflowExecutionContext()
        assert changed.compute(subject, run_context=context)["value"].tolist() == [13]
        outcome = _outcome(context)
        assert outcome.result_key != outcomes[0].result_key
        assert (outcome.result_key, outcome.record_id) == (outcomes[2].result_key, outcomes[2].record_id)
        plan = changed.plan()["subject"]
        assert plan.status is NodePlanStatus.CACHED
        assert (plan.final_result_key, plan.selected_record_id) == (outcome.result_key, outcome.record_id)
        assert BoundDefaultValues.executions == 3
    finally:
        BoundDefaultValues.Inputs.payload = [4]
        for owner in owners:
            owner.close()


@pytest.mark.parametrize("family", ["source-processing", "bound-processing", "dataframe"])
def test_actual_field_types_select_distinct_records_and_reuse_unchanged(tmp_path, family):
    if family == "source-processing":
        tool = SourceContainerKind
    elif family == "bound-processing":
        tool = BoundContainerKind
    else:
        tool = DictionaryKeyKind
    tool.executions = 0
    parameters = [{4: "value"}, {"4": "value"}, {4: "value"}] if family == "dataframe" else [[4], (4,), [4]]
    kinds = []
    results = []
    contexts = []
    owners = []
    try:
        for payload in parameters:
            context = WorkflowExecutionContext()
            with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
                if family == "source-processing":
                    node = tool()(payload=payload, name="subject")
                else:
                    seed = SeedValue()(name="seed")
                    node = tool()(seed, payload=payload, name="subject") if family == "dataframe" else tool()(value=seed["value"], payload=payload, name="subject")
            owners.append(workflow.shared_memory_context)
            result = workflow.compute(node, run_context=context)
            kinds.append(result["observed_kind"].tolist())
            results.append(result["value"].tolist())
            contexts.append(context)
        expected = ["int", "str", "int"] if family == "dataframe" else ["list", "tuple", "list"]
        a, b, repeated = map(_outcome, contexts)
        observed = {"kinds": kinds, "executions": tool.executions, "selected": [(item.result_key, item.record_id) for item in (a, b, repeated)]}
        assert kinds == [[kind] for kind in expected], observed
        value = 8 if family == "bound-processing" else 4
        assert results == [[value], [value], [value]]
        assert tool.executions == 2
        assert all(item.result_key is not None and item.record_id is not None for item in (a, b, repeated))
        assert a.result_key != b.result_key
        assert (a.result_key, a.record_id) == (repeated.result_key, repeated.record_id)
    finally:
        for owner in owners:
            owner.close()


def test_omitted_effective_default_and_equal_explicit_value_reuse_one_record(tmp_path):
    SourceContainerKind.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    owners = []
    try:
        for index, parameters in enumerate(({}, {"payload": [4]})):
            with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
                node = SourceContainerKind()(name="subject", **parameters)
            owners.append(workflow.shared_memory_context)
            frame = workflow.compute(node, run_context=contexts[index])
            assert frame["observed_kind"].tolist() == ["list"]
            assert frame["value"].tolist() == [4]
        assert SourceContainerKind.executions == 1
        a, b = map(_outcome, contexts)
        assert a.result_key is not None and a.record_id is not None
        assert (a.result_key, a.record_id) == (b.result_key, b.record_id)
    finally:
        for owner in owners:
            owner.close()
