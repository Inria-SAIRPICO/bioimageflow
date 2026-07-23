"""Separate backend-task diagnostic storage tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bioimageflow.storage import CacheCorruptionError, Storage


RUN_ID = "run_" + "1" * 32
INVOCATION_ID = "inv_" + "2" * 32
ATTEMPT_ID = "att_" + "3" * 32
TASK_ID = "task_" + "4" * 16


def test_backend_task_diagnostic_is_separate_and_terminalized(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)

    path = storage.start_backend_task_diagnostic(
        RUN_ID,
        "nested/Segment_1",
        INVOCATION_ID,
        TASK_ID,
        backend="parsl",
        executor_label="gpu",
        cache_attempt_id=ATTEMPT_ID,
        task_retry=0,
        mode="row_chunk",
        row_positions=[2, 3],
        tool_origin={
            "schema": "bioimageflow.worker_origin.v1",
            "kind": "installed_module",
        },
    )

    assert path.relative_to(tmp_path).parts[:3] == (
        "diagnostics",
        "v1",
        "runs",
    )
    submitted = json.loads(path.read_text())
    assert submitted["status"] == "submitted"
    assert submitted["completed_at"] is None
    assert submitted["row_positions"] == [2, 3]

    storage.finish_backend_task_diagnostic(
        RUN_ID,
        "nested/Segment_1",
        INVOCATION_ID,
        TASK_ID,
        status="failed",
        error_type="RuntimeError",
    )

    terminal = json.loads(path.read_text())
    assert terminal["status"] == "failed"
    assert terminal["completed_at"] is not None
    assert terminal["error_type"] == "RuntimeError"


def test_backend_task_diagnostic_rejects_unsafe_identity_and_reterminalization(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    with pytest.raises(ValueError):
        storage.backend_task_diagnostic_path(
            RUN_ID,
            "../escape",
            INVOCATION_ID,
            TASK_ID,
        )

    storage.start_backend_task_diagnostic(
        RUN_ID,
        "Segment_1",
        INVOCATION_ID,
        TASK_ID,
        backend="parsl",
        executor_label="cpu",
        cache_attempt_id=None,
        task_retry=0,
        mode="process_batch",
        row_positions=[],
        tool_origin={},
    )
    storage.finish_backend_task_diagnostic(
        RUN_ID,
        "Segment_1",
        INVOCATION_ID,
        TASK_ID,
        status="succeeded",
    )

    with pytest.raises(CacheCorruptionError, match="already terminal"):
        storage.finish_backend_task_diagnostic(
            RUN_ID,
            "Segment_1",
            INVOCATION_ID,
            TASK_ID,
            status="succeeded",
        )
