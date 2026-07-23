"""Run-scoped transient processing workspace tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bioimageflow.storage import CacheCorruptionError, Storage


RUN_ID = "run_0123456789abcdef0123456789abcdef"


def test_transient_invocation_has_exact_identity_and_terminal_metadata(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)

    invocation_id, invocation_dir, assets_dir = storage.create_transient_invocation(
        RUN_ID,
        "nested/Process_1",
        engine="direct:parallel",
    )

    assert re.fullmatch(r"inv_[0-9a-f]{32}", invocation_id)
    assert invocation_dir == (
        tmp_path
        / "cache"
        / "v1"
        / "transient"
        / "runs"
        / RUN_ID
        / "nodes"
        / "nested"
        / "Process_1"
        / invocation_id
    )
    assert assets_dir == invocation_dir / "assets"
    assert (invocation_dir / "work").is_dir()
    running = json.loads((invocation_dir / "invocation.json").read_text())
    assert running["status"] == "running"
    assert running["completed_at"] is None

    error = RuntimeError("worker failed")
    storage.finish_transient_invocation(
        RUN_ID,
        "nested/Process_1",
        invocation_id,
        status="failed",
        error=error,
    )

    terminal = json.loads((invocation_dir / "invocation.json").read_text())
    assert terminal["status"] == "failed"
    assert terminal["completed_at"] is not None
    failure = json.loads((invocation_dir / "failed.json").read_text())
    assert failure["type"] == "RuntimeError"
    assert failure["message"] == "worker failed"
    assert not (tmp_path / "cache" / "v1" / "results").exists()
    assert not (tmp_path / "views").exists()
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize(
    ("run_id", "node_key", "invocation_id"),
    [
        ("run_short", "node", "inv_" + "1" * 32),
        (RUN_ID, "../node", "inv_" + "1" * 32),
        (RUN_ID, "node", "inv_short"),
    ],
)
def test_transient_invocation_rejects_unsafe_identifiers(
    tmp_path: Path,
    run_id: str,
    node_key: str,
    invocation_id: str,
) -> None:
    storage = Storage(tmp_path)

    with pytest.raises(ValueError):
        storage.transient_invocation_dir(run_id, node_key, invocation_id)


def test_transient_invocation_rejects_symlinked_node_path(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "storage")
    outside = tmp_path / "outside"
    outside.mkdir()
    nodes = (
        storage.cache_root
        / "transient"
        / "runs"
        / RUN_ID
        / "nodes"
    )
    nodes.parent.mkdir(parents=True)
    nodes.symlink_to(outside)

    with pytest.raises(CacheCorruptionError, match="real directories"):
        storage.create_transient_invocation(
            RUN_ID,
            "Process_1",
            engine="direct:parallel",
        )

    assert list(outside.iterdir()) == []
