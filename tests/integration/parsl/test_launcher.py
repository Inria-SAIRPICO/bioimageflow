from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import (
    DataFrameTool,
    ExecutorBinding,
    ExecutorCapabilities,
    OrchestratorLaunchConfig,
    ParslConfigRef,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    WorkflowRun,
    submit_workflow,
)
from bioimageflow.cache import compute_env_hash
from bioimageflow.launcher.errors import WorkflowRunFailedError
from bioimageflow_core import IOModel
from tests.testkit.parsl_tools import (
    PARSL_TEST_ENV,
    ParslFail,
    ParslIncrement,
)


class _ArchiveSource(DataFrameTool):
    accepts_upstream = False

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": [7]}, index=["archive"])


class _IdentityTable(DataFrameTool):
    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame(df)


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name=PARSL_TEST_ENV.name,
                dependency_hash=compute_env_hash(
                    PARSL_TEST_ENV.dependencies
                ),
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.1.7,<0.2",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=(
                "installed_module",
                "shared_module",
                "archive_module",
            ),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _config_ref(storage_path: Path) -> ParslConfigRef:
    return ParslConfigRef(
        "tests.unit.launcher.config_factories:build_threads",
        {
            "max_threads": 2,
            "run_dir": str(storage_path / "parsl-runinfo"),
        },
    )


def _wait_terminal(run: WorkflowRun) -> None:
    deadline = time.monotonic() + 20
    while run.status not in {"succeeded", "failed", "cancelled", "lost"}:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Local orchestrator timed out in {run.status!r}.\n{run.logs()}"
            )
        time.sleep(0.02)
        run.refresh()


def test_local_launcher_runs_in_separate_process_and_reconnects(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    run = submit_workflow(
        workflow,
        parsl_config=_config_ref(tmp_path),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="local"),
    )

    _wait_terminal(run)

    assert run.status == "succeeded", (
        run.logs(),
        (run.control_dir / "error.json").read_text()
        if (run.control_dir / "error.json").is_file()
        else "",
    )
    assert run.control_dir == tmp_path / "launcher" / "v1" / "runs" / run.id
    assert run.view_dir == tmp_path / "views" / "runs" / run.id
    reopened = WorkflowRun.open(tmp_path, run.id)
    result = reopened.result()
    assert isinstance(result, pd.DataFrame)
    assert result.empty


@pytest.mark.parametrize(
    ("input_kind", "input_value", "expected"),
    [
        ("scalar", 4, {"0": 5}),
        (
            "dataframe",
            pd.DataFrame(
                {"value": [2, 8]},
                index=["first", "second"],
            ),
            {"first": 3, "second": 9},
        ),
    ],
)
def test_submitted_root_inputs_execute_and_reconnect(
    tmp_path: Path,
    input_kind: str,
    input_value: object,
    expected: dict[str, int],
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        if input_kind == "scalar":
            value = workflow.input("value", int, id="input-value")
            incremented = ParslIncrement()(
                value=value,
                name="increment",
            )
        else:
            table = workflow.input(
                "table",
                kind="dataframe",
                id="input-table",
            )
            identity = _IdentityTable()(table, name="identity")
            incremented = ParslIncrement()(
                value=identity["value"],
                name="increment",
            )
        workflow.output(
            "result",
            incremented["value"],
            id="output-result",
        )
    inputs = {
        "value" if input_kind == "scalar" else "table": input_value
    }

    run = submit_workflow(
        workflow,
        inputs=inputs,
        parsl_config=_config_ref(tmp_path),
        executor_bindings={"threads": _binding()},
        shared_runtime_root=tmp_path / "runtime",
        launch=OrchestratorLaunchConfig(backend="local"),
    )
    _wait_terminal(run)

    assert run.status == "succeeded", (
        run.logs(),
        (run.control_dir / "error.json").read_text()
        if (run.control_dir / "error.json").is_file()
        else "",
    )
    result = WorkflowRun.open(tmp_path, run.id).result()
    assert result["result"].to_dict() == expected
    public = [
        entry["payload"]
        for entry in run.progress()
        if entry["kind"] == "public"
    ]
    assert public
    assert public[-1]["status"] == "completed"


def test_submitted_ad_hoc_targets_preserve_requested_order(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        ParslIncrement()(value=1, name="first")
        ParslIncrement()(value=9, name="second")

    run = submit_workflow(
        workflow,
        targets=["second", "first"],
        parsl_config=_config_ref(tmp_path),
        executor_bindings={"threads": _binding()},
        shared_runtime_root=tmp_path / "runtime",
        launch=OrchestratorLaunchConfig(backend="local"),
    )
    _wait_terminal(run)

    assert run.status == "succeeded", run.logs()
    result = run.result()
    assert list(result) == ["second", "first"]
    assert result["second"].at["0", "value"] == 10
    assert result["first"].at["0", "value"] == 2


def test_submitted_archive_custom_source_executes(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        source = _ArchiveSource()(name="archive-source")
        workflow.output(
            "value",
            source["value"],
            id="output-value",
        )

    run = submit_workflow(
        workflow,
        parsl_config=_config_ref(tmp_path),
        executor_bindings={"threads": _binding()},
        shared_runtime_root=tmp_path / "runtime",
        launch=OrchestratorLaunchConfig(backend="local"),
    )
    _wait_terminal(run)

    assert run.status == "succeeded", run.logs()
    assert run.result().at["archive", "value"] == 7


def test_submitted_remote_failure_and_progress_are_persisted(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        ParslFail()(value=13, name="remote-failure")

    run = submit_workflow(
        workflow,
        targets=["remote-failure"],
        parsl_config=_config_ref(tmp_path),
        executor_bindings={"threads": _binding()},
        shared_runtime_root=tmp_path / "runtime",
        launch=OrchestratorLaunchConfig(backend="local"),
    )
    _wait_terminal(run)

    assert run.status == "failed", run.logs()
    with pytest.raises(WorkflowRunFailedError) as captured:
        run.result()
    assert "remote failure 13" in str(captured.value)
    statuses = [
        entry["payload"]["status"]
        for entry in run.progress()
        if entry["kind"] == "public"
    ]
    assert statuses[0] == "started"
    assert statuses[-1] == "failed"


def test_submitted_factory_secret_is_redacted_from_error_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "literal-integration-secret"
    monkeypatch.setenv("BIF_INTEGRATION_CREDENTIAL", secret)
    run = submit_workflow(
        Workflow(storage_path=tmp_path, engine="direct"),
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:"
            "print_and_fail_with_credential",
            {},
            secret_refs={
                "credential": "BIF_INTEGRATION_CREDENTIAL",
            },
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="local"),
    )
    _wait_terminal(run)

    assert run.status == "failed"
    assert secret not in run.logs()
    assert secret not in (run.control_dir / "error.json").read_text()
    assert "[REDACTED]" in run.logs()
