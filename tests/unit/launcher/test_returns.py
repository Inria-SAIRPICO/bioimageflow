from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import (
    DataFrameTool,
    Passthrough,
    Workflow,
    WorkflowExecutionContext,
)
from bioimageflow.launcher.errors import (
    LauncherProtocolError,
    WorkflowRunResultUnavailableError,
)
from bioimageflow.launcher.inputs import InvocationOutput, LoadedInvocation
from bioimageflow.launcher.return_routes import (
    DeclaredReturnColumn,
    ReturnProviderRoute,
    ReturnRoutePlan,
    build_return_provider_routes,
)
from bioimageflow.launcher.returns import (
    load_public_return,
    persist_public_return,
)
from bioimageflow.storage import Storage
from bioimageflow.workflow.execution_context import ExecutionProviderOutcome
from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    RowConsumption,
)
from bioimageflow_core.types import SharedArray
from tests.testkit.runtime_cache import SourceAssetWriter


RUN_ID = "run_1234567812344abc923456789abcdef0"


class _PassthroughRows(DataFrameTool):
    class Outputs(Passthrough):
        pass


class _SuffixMerge(DataFrameTool):
    class Outputs(Passthrough):
        pass

    def merge_dataframes(self, dfs, arguments):
        del arguments
        return dfs[0].join(dfs[1], rsuffix="_right")

    @classmethod
    def resolve_merge_schema(cls, upstream_schemas, inputs=None):
        del inputs
        left, right = upstream_schemas
        result = dict(left)
        result.update(
            {
                f"{column}_right" if column in left else column: entry
                for column, entry in right.items()
            }
        )
        return result


class _SharedArrayWriter(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="return_shared_array_writer", dependencies={})
    created_names: list[str] = []

    class Inputs(IOModel):
        value: int = 5

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


def _control_dir(tmp_path: Path) -> Path:
    control = tmp_path / "launcher" / "v1" / "runs" / RUN_ID
    control.mkdir(parents=True)
    return control


def test_single_external_path_return_round_trips(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    external = (tmp_path / "external.tif").resolve()
    value = pd.DataFrame(
        {"path": [external], "count": [3]},
        index=["row"],
    )

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        value,
        outcomes=(),
        root_outputs=[{"port_id": "output-image", "name": "path"}],
        provider_routes=(
            ReturnProviderRoute(
                mapping_key=None,
                public_column="path",
                node_key="source",
                provider_column="path",
                result_key=None,
                record_id=None,
                transient_invocation_id=None,
                owned=False,
                shared_array=False,
            ),
        ),
    )
    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert loaded.at["row", "path"] == external
    assert loaded.at["row", "count"] == 3


def test_mapping_shape_and_key_order_round_trip(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    value = {
        "second": pd.DataFrame({"value": [2]}, index=["b"]),
        "first": pd.DataFrame({"value": [1]}, index=["a"]),
    }

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        value,
        outcomes=(),
    )
    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert list(loaded) == ["second", "first"]
    assert loaded["first"].at["a", "value"] == 1


def _engine_path_return(
    tmp_path: Path,
) -> tuple[Path, dict, WorkflowExecutionContext]:
    workflow = Workflow(
        storage_path=tmp_path,
        engine="direct",
        name="published",
    )
    with workflow:
        writer = SourceAssetWriter()(name="writer")
        workflow.output(
            "renamed_mask",
            writer["mask"],
            id="output-mask",
        )
    context = WorkflowExecutionContext(
        run_id=RUN_ID,
        defer_success_finalization=True,
    )
    result = workflow.compute(inputs={}, run_context=context)
    control = _control_dir(tmp_path)
    invocation = LoadedInvocation(
        variant="root",
        inputs={},
        targets=(),
        outputs=(
            InvocationOutput(
                port_id="output-mask",
                name="renamed_mask",
            ),
        ),
    )
    manifest = persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        result,
        outcomes=context.execution_outcomes,
        root_outputs=invocation.outputs,
        provider_routes=build_return_provider_routes(
            workflow,
            invocation,
            context.execution_outcomes,
        ),
    )
    return control, manifest, context


def test_record_asset_return_uses_exact_immutable_record(tmp_path: Path) -> None:
    control, manifest, context = _engine_path_return(tmp_path)
    storage = Storage(tmp_path)
    outcome = context.execution_outcomes[0]
    assert outcome.result_key is not None
    assert outcome.record_id is not None
    current = storage.result_dir(outcome.result_key) / "current.json"
    current.unlink()

    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert Path(loaded.at["0", "renamed_mask"]).is_file()
    assert manifest["locators"][0]["result_key"] == outcome.result_key
    assert manifest["locators"][0]["record_id"] == outcome.record_id


def test_pruned_record_raises_result_unavailable(tmp_path: Path) -> None:
    control, _manifest, context = _engine_path_return(tmp_path)
    storage = Storage(tmp_path)
    outcome = context.execution_outcomes[0]
    assert outcome.result_key is not None
    assert outcome.record_id is not None
    record_dir = (
        storage.result_dir(outcome.result_key)
        / "records"
        / outcome.record_id
    )
    renamed = record_dir.with_name(f"{outcome.record_id}.pruned")
    record_dir.rename(renamed)

    with pytest.raises(
        WorkflowRunResultUnavailableError,
        match="Immutable record",
    ):
        load_public_return(control, tmp_path, RUN_ID)


def test_transient_return_asset_is_self_contained(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    storage = Storage(tmp_path)
    invocation_id, invocation_dir, assets_dir = storage.create_transient_invocation(
        RUN_ID,
        "writer",
        engine="direct:sequential",
    )
    source = assets_dir / "mask.tif"
    source.write_bytes(b"transient-mask")
    storage.finish_transient_invocation(
        RUN_ID,
        "writer",
        invocation_id,
        status="succeeded",
    )
    outcome = ExecutionProviderOutcome(
        node_key="writer",
        result_key=None,
        record_id=None,
        transient_invocation_id=invocation_id,
        path_columns=("mask",),
        owned_path_columns=("mask",),
        shared_array_columns=(),
    )

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        pd.DataFrame({"mask": [source]}, index=["row"]),
        outcomes=(outcome,),
        provider_routes=(
            ReturnProviderRoute(
                mapping_key=None,
                public_column="mask",
                node_key="writer",
                provider_column="mask",
                result_key=None,
                record_id=None,
                transient_invocation_id=invocation_id,
                owned=True,
                shared_array=False,
            ),
        ),
    )
    moved = invocation_dir.with_name(f"{invocation_dir.name}.removed")
    invocation_dir.rename(moved)
    loaded = load_public_return(control, tmp_path, RUN_ID)

    returned = loaded.at["row", "mask"]
    assert isinstance(returned, Path)
    assert returned.read_bytes() == b"transient-mask"
    assert control in returned.parents


def test_passthrough_target_keeps_upstream_record_provenance(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        writer = SourceAssetWriter()(name="writer")
        filtered = _PassthroughRows()(writer, name="filtered")
    context = WorkflowExecutionContext(
        run_id=RUN_ID,
        defer_success_finalization=True,
    )
    result = workflow.compute(filtered, run_context=context)
    invocation = LoadedInvocation(
        variant="targets",
        inputs={},
        targets=("filtered",),
        outputs=(),
    )
    control = _control_dir(tmp_path)

    manifest = persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        result,
        outcomes=context.execution_outcomes,
        provider_routes=build_return_provider_routes(
            workflow,
            invocation,
            context.execution_outcomes,
        ),
    )

    outcomes = {
        outcome.node_key: outcome for outcome in context.execution_outcomes
    }
    locator = manifest["locators"][0]
    assert locator["column"] == "mask"
    assert locator["result_key"] == outcomes["writer"].result_key
    assert locator["record_id"] == outcomes["writer"].record_id


def test_merge_target_routes_renamed_paths_to_each_exact_provider(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        left = SourceAssetWriter()(text="left", name="left")
        right = SourceAssetWriter()(text="right", name="right")
        merged = _SuffixMerge()(left, right, name="merged")
    context = WorkflowExecutionContext(
        run_id=RUN_ID,
        defer_success_finalization=True,
    )
    result = workflow.compute(merged, run_context=context)
    invocation = LoadedInvocation(
        variant="targets",
        inputs={},
        targets=("merged",),
        outputs=(),
    )
    control = _control_dir(tmp_path)

    manifest = persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        result,
        outcomes=context.execution_outcomes,
        provider_routes=build_return_provider_routes(
            workflow,
            invocation,
            context.execution_outcomes,
        ),
    )

    outcomes = {
        outcome.node_key: outcome for outcome in context.execution_outcomes
    }
    locators = {
        locator["column"]: locator for locator in manifest["locators"]
    }
    assert locators["mask"]["record_id"] == outcomes["left"].record_id
    assert locators["mask_right"]["record_id"] == outcomes["right"].record_id


@pytest.mark.shared_memory
def test_record_backed_shared_array_uses_raw_exact_record_locator(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    _SharedArrayWriter.created_names = []
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        writer = _SharedArrayWriter()(name="writer")
        workflow.output("renamed_image", writer["image"], id="image-output")
    context = WorkflowExecutionContext(
        run_id=RUN_ID,
        defer_success_finalization=True,
    )
    result = workflow.compute(inputs={}, run_context=context)
    invocation = LoadedInvocation(
        variant="root",
        inputs={},
        targets=(),
        outputs=(
            InvocationOutput(
                port_id="image-output",
                name="renamed_image",
            ),
        ),
    )
    control = _control_dir(tmp_path)
    loaded_reference = None
    returned_reference = result.at["0", "renamed_image"]
    try:
        manifest = persist_public_return(
            control,
            tmp_path,
            RUN_ID,
            result,
            outcomes=context.execution_outcomes,
            root_outputs=invocation.outputs,
            provider_routes=build_return_provider_routes(
                workflow,
                invocation,
                context.execution_outcomes,
            ),
        )
        [outcome] = context.execution_outcomes
        [locator] = manifest["locators"]
        assert locator["kind"] == "record_asset"
        assert locator["result_key"] == outcome.result_key
        assert locator["record_id"] == outcome.record_id
        assert locator["shared_array"]["shape"] == [2, 2]

        loaded = load_public_return(control, tmp_path, RUN_ID)
        loaded_reference = loaded.at["0", "renamed_image"]
        with open_shared_array(loaded_reference) as array:
            assert array.tolist() == [[5, 5], [5, 5]]
    finally:
        names = {
            *_SharedArrayWriter.created_names,
            returned_reference.name,
        }
        if loaded_reference is not None:
            names.add(loaded_reference.name)
        _unlink_shared_memory(names)


def test_declared_path_string_without_provider_route_fails_closed(
    tmp_path: Path,
) -> None:
    control = _control_dir(tmp_path)
    route_plan = ReturnRoutePlan(
        routes=(),
        declared_columns=(
            DeclaredReturnColumn(
                mapping_key=None,
                public_column="path",
                path=True,
                shared_array=False,
            ),
        ),
    )

    with pytest.raises(
        LauncherProtocolError,
        match="Declared path return cell",
    ):
        persist_public_return(
            control,
            tmp_path,
            RUN_ID,
            pd.DataFrame(
                {"path": [str((tmp_path / "untracked.tif").resolve())]},
                index=["row"],
            ),
            outcomes=(),
            provider_routes=route_plan,
        )
