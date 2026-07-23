"""Reusable cache-attempt metadata lifecycle tests."""

from __future__ import annotations

import json
from pathlib import Path

from bioimageflow.storage import Storage, make_result_key


def test_cache_attempt_metadata_carries_run_and_terminal_state(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "Segment_1"})
    attempt_id = storage.new_attempt_id()
    run_id = "run_" + "1" * 32
    invocation_id = "inv_" + "2" * 32

    path = storage.start_cache_attempt(
        result_key,
        attempt_id,
        run_id=run_id,
        node_key="nested/Segment_1",
        invocation_id=invocation_id,
        tool_identity="tools.segment:Segment",
        engine="parsl:parallel",
    )

    running = json.loads(path.read_text())
    assert running["status"] == "running"
    assert running["run_id"] == run_id
    assert running["invocation_id"] == invocation_id
    assert running["engine"] == "parsl:parallel"
    assert running["worker_identity"] is None

    storage.finish_cache_attempt(
        result_key,
        attempt_id,
        status="failed",
        error_type="RuntimeError",
    )

    terminal = json.loads(path.read_text())
    assert terminal["status"] == "failed"
    assert terminal["completed_at"] is not None
    assert terminal["error_type"] == "RuntimeError"
