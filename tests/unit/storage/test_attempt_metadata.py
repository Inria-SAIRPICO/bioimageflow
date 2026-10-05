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


def test_repeated_terminal_attempt_write_requires_identical_facts(tmp_path):
    import pytest
    from bioimageflow.storage import CacheCorruptionError

    storage = Storage(tmp_path)
    key = make_result_key({"node": "repeated"})
    attempt = storage.new_attempt_id()
    path = storage.start_cache_attempt(
        key, attempt, run_id="run_" + "3" * 32,
        node_key="repeated", tool_identity="tool", engine="direct",
    )
    storage.finish_cache_attempt(key, attempt, status="failed", error_type="ValueError")
    prior = path.read_bytes()
    storage.finish_cache_attempt(key, attempt, status="failed", error_type="ValueError")
    assert path.read_bytes() == prior
    with pytest.raises(CacheCorruptionError, match="already terminal"):
        storage.finish_cache_attempt(key, attempt, status="succeeded")
    assert path.read_bytes() == prior
