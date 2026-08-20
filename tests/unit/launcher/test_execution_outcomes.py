"""Root execution outcomes used by submitted return persistence."""

from pathlib import Path

import pytest

from bioimageflow import SequentialEngine, Workflow, WorkflowExecutionContext
from bioimageflow.storage import CacheCorruptionError, Storage
from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    RowConsumption,
    Template,
)
from bioimageflow_core.types import SharedArray
from tests.testkit.runtime_cache import CountingTable, SourceAssetWriter


class _TransientAssetWriter(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="transient_outcome_writer", dependencies={})

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        asset: Path = Template("value_{row_index}.txt")

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ):
        assert context is not None
        asset = Path(arguments.asset)
        asset.write_text(str(arguments.value))
        return self.Outputs(asset=asset)


class _SharedArrayWriter(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="outcome_shared_array_writer", dependencies={})
    created_names: list[str] = []

    class Inputs(IOModel):
        value: int = 3

    class Outputs(IOModel):
        image: SharedArray

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ):
        import numpy as np
        from bioimageflow_core.shm import create_shared_output

        del context
        with create_shared_output(
            np.full((2, 2), arguments.value, dtype=np.uint8)
        ) as reference:
            type(self).created_names.append(reference.name)
            return self.Outputs(image=reference)


def _unlink_shared_memory(names: set[str]) -> None:
    from multiprocessing.shared_memory import SharedMemory

    for name in names:
        try:
            shared = SharedMemory(name=name)
        except FileNotFoundError:
            continue
        shared.close()
        shared.unlink()


def test_scoped_record_outcome_loads_exact_record_without_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "root-results"
    child = Workflow(
        name="child",
        storage_path=tmp_path / "child-results",
        engine="direct",
    )
    with child:
        writer = SourceAssetWriter()(text="exact", name="writer")
        child.output("mask", writer["mask"], id="mask-output")
        child.output("count", writer["count"], id="count-output")

    root = Workflow(name="root", storage_path=storage_path, engine="direct")
    with root:
        nested = child(name="nested")
        root.output("mask", nested["mask"], id="root-mask-output")
        root.output("count", nested["count"], id="root-count-output")

    context = WorkflowExecutionContext("run_0123456789abcdef0123456789abcdef")
    result = root.compute(run_context=context)

    [outcome] = context.execution_outcomes
    assert outcome.node_key == "nested/writer"
    assert outcome.storage_kind == "record"
    assert outcome.result_key is not None
    assert outcome.record_id is not None
    assert outcome.transient_invocation_id is None
    assert outcome.path_columns == ("mask",)
    assert outcome.owned_path_columns == ("mask",)
    assert outcome.shared_array_columns == ()

    storage = Storage(storage_path)

    def fail_current_lookup(*args, **kwargs):
        raise AssertionError("exact record loading must not consult current.json")

    monkeypatch.setattr(storage, "load_current", fail_current_lookup)
    raw = storage.load_record_dataframe(
        outcome.result_key,
        outcome.record_id,
        path_columns=outcome.path_columns,
    )
    raw_asset = str(raw.loc["0", "mask"])
    assert raw_asset.startswith("assets/")

    hydrated = storage.load_record_dataframe(
        outcome.result_key,
        outcome.record_id,
        path_columns=outcome.path_columns,
        hydrate_assets=True,
    )
    hydrated_asset = Path(hydrated.loc["0", "mask"])
    assert hydrated_asset.read_text() == "exact"
    assert Path(result.loc["0", "mask"]).resolve() == hydrated_asset.resolve()
    assert (
        storage.resolve_record_asset(
            outcome.result_key,
            outcome.record_id,
            raw_asset,
        ).resolve()
        == hydrated_asset.resolve()
    )
    with pytest.raises(CacheCorruptionError, match="not named"):
        storage.resolve_record_asset(
            outcome.result_key,
            outcome.record_id,
            "assets/missing.txt",
        )


@pytest.mark.shared_memory
def test_exact_record_rehydrates_declared_shared_array_outcome(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    _SharedArrayWriter.created_names = []
    storage_path = tmp_path / "results"
    workflow = Workflow(storage_path=storage_path, engine="direct")
    with workflow:
        writer = _SharedArrayWriter()(value=6, name="writer")
    context = WorkflowExecutionContext()

    result = workflow.compute(writer, run_context=context)
    [outcome] = context.execution_outcomes
    assert outcome.storage_kind == "record"
    assert outcome.result_key is not None
    assert outcome.record_id is not None
    assert outcome.path_columns == ()
    assert outcome.shared_array_columns == ("image",)

    storage = Storage(storage_path)
    raw = storage.load_record_dataframe(
        outcome.result_key,
        outcome.record_id,
        shared_array_columns=outcome.shared_array_columns,
    )
    assert str(raw.loc["0", "image"]).startswith("assets/shm/")
    hydrated = storage.load_record_dataframe(
        outcome.result_key,
        outcome.record_id,
        shared_array_columns=outcome.shared_array_columns,
        hydrate_assets=True,
    )
    returned_reference = result.loc["0", "image"]
    exact_reference = hydrated.loc["0", "image"]
    try:
        with open_shared_array(exact_reference) as array:
            assert array.tolist() == [[6, 6], [6, 6]]
    finally:
        _unlink_shared_memory(
            {
                *_SharedArrayWriter.created_names,
                returned_reference.name,
                exact_reference.name,
            }
        )


def test_transient_processing_outcome_records_exact_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "results"
    workflow = Workflow(storage_path=storage_path, engine="direct")
    with workflow:
        source = CountingTable()(value=9, name="source")
        writer = _TransientAssetWriter()(value=source["value"], name="writer")

    engine = SequentialEngine()
    monkeypatch.setattr(
        engine,
        "_compute_processing_sig_hash",
        lambda *args, **kwargs: None,
    )
    context = WorkflowExecutionContext("run_abcdef0123456789abcdef0123456789")

    result = workflow.compute(writer, engine=engine, run_context=context)

    outcomes = {outcome.node_key: outcome for outcome in context.execution_outcomes}
    outcome = outcomes["writer"]
    assert outcome.storage_kind == "transient"
    assert outcome.result_key is None
    assert outcome.record_id is None
    assert outcome.transient_invocation_id is not None
    assert outcome.path_columns == ("asset",)
    assert outcome.owned_path_columns == ("asset",)
    assert outcome.shared_array_columns == ()

    invocation_dir = Storage(storage_path).transient_invocation_dir(
        context.run_id,
        "writer",
        outcome.transient_invocation_id,
    )
    assert invocation_dir.is_dir()
    assert Path(result.loc["row", "asset"]).parent == invocation_dir / "assets"


def test_outcome_catalog_is_ordered_idempotent_and_tracks_shared_kinds() -> None:
    context = WorkflowExecutionContext()
    context._bind(object(), on_success=lambda: None, on_failure=lambda error: None)
    invocation_id = "inv_0123456789abcdef0123456789abcdef"

    context._record_provider_outcome(
        node_key="z/provider",
        result_key=None,
        record_id=None,
        transient_invocation_id=invocation_id,
        path_columns={"image"},
        owned_path_columns={"image"},
        shared_array_columns={"image"},
    )
    context._record_provider_outcome(
        node_key="a/provider",
        result_key="rk_exact",
        record_id="rec_exact",
        transient_invocation_id=None,
        path_columns=set(),
        owned_path_columns=set(),
        shared_array_columns=set(),
    )
    context._record_provider_outcome(
        node_key="z/provider",
        result_key=None,
        record_id=None,
        transient_invocation_id=invocation_id,
        path_columns={"image"},
        owned_path_columns={"image"},
        shared_array_columns={"image"},
    )

    outcomes = context.execution_outcomes
    assert [outcome.node_key for outcome in outcomes] == [
        "a/provider",
        "z/provider",
    ]
    assert outcomes[1].shared_array_columns == ("image",)
    with pytest.raises(RuntimeError, match="inconsistently"):
        context._record_provider_outcome(
            node_key="z/provider",
            result_key=None,
            record_id=None,
            transient_invocation_id=invocation_id,
            path_columns={"other"},
            owned_path_columns=set(),
            shared_array_columns=set(),
        )
