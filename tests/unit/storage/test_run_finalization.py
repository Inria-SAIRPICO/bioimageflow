from pathlib import Path

import pytest

from bioimageflow.storage import CacheCorruptionError, Storage


RUN_ID = "run_1234567812344abc923456789abcdef0"


def _start(storage: Storage) -> None:
    storage.write_run_metadata(
        RUN_ID,
        workflow_identity="workflow:test",
        engine="parsl:parallel",
        status="running",
        target_nodes=["target"],
        started_at="2026-07-28T12:00:00+00:00",
    )


def test_canonical_success_finalization_is_idempotent(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    _start(storage)

    first = storage.finalize_run_metadata(
        RUN_ID,
        status="succeeded",
        update_latest_success=True,
        completed_at="2026-07-28T12:01:00+00:00",
    )
    second = storage.finalize_run_metadata(
        RUN_ID,
        status="succeeded",
        update_latest_success=True,
        completed_at="2026-07-28T12:02:00+00:00",
    )

    assert first == second
    assert storage.latest_success_run_id() == RUN_ID
    assert storage._load_run_metadata(RUN_ID)["completed_at"] == (
        "2026-07-28T12:01:00+00:00"
    )


def test_canonical_terminal_state_cannot_be_rewritten(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    _start(storage)
    storage.finalize_run_metadata(
        RUN_ID,
        status="failed",
        update_latest_success=False,
    )

    with pytest.raises(CacheCorruptionError, match="already terminal"):
        storage.finalize_run_metadata(
            RUN_ID,
            status="succeeded",
            update_latest_success=True,
        )
