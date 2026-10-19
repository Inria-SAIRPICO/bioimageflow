"""Small published-Wetlands smoke test.

This is the lightweight certification tier for the real ProcessingTool ->
Wetlands worker path. The broader task API suite remains in
test_wetlands_task_api.py.
"""

from __future__ import annotations

import os
import logging
import json
import re
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from packaging.version import Version

from bioimageflow import WetlandsEnvManager, Workflow, configure_logging
from bioimageflow.env_manager import _reset_shared_manager

from tests.testkit.integration_tools import FileLoader
from tests.testkit.wetlands_tools import SimpleRowTool, WorkerStreamTool

pytestmark = pytest.mark.wetlands


def _snapshot_loggers() -> dict[str, tuple[list[logging.Handler], int, bool]]:
    return {
        name: (logger.handlers[:], logger.level, logger.propagate)
        for name in ("bioimageflow", "wetlands")
        for logger in [logging.getLogger(name)]
    }


def _restore_loggers(
    snapshot: dict[str, tuple[list[logging.Handler], int, bool]],
) -> None:
    for name, (handlers, level, propagate) in snapshot.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture(autouse=True)
def _disable_wetlands() -> Iterator[None]:
    """Override the integration conftest patch that disables Wetlands."""
    yield


@pytest.fixture(autouse=True)
def _isolated_wetlands_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    home = Path(os.environ.get("BIOIMAGEFLOW_HOME", tmp_path / "bioimageflow-home"))
    wetlands_root = Path(os.environ.get("BIOIMAGEFLOW_WETLANDS", home / "wetlands"))
    tool_store = Path(os.environ.get("BIOIMAGEFLOW_TOOL_STORE", home / "tool_packages"))

    monkeypatch.setenv("BIOIMAGEFLOW_HOME", str(home))
    monkeypatch.setenv("BIOIMAGEFLOW_WETLANDS", str(wetlands_root))
    monkeypatch.setenv("BIOIMAGEFLOW_TOOL_STORE", str(tool_store))

    _reset_shared_manager()
    yield
    _reset_shared_manager()


def test_processing_tool_executes_through_published_wetlands(tmp_path: Path) -> None:
    assert Version(version("wetlands")) >= Version("1.0.1")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ["img_01.tif", "img_02.tif", "img_03.tif"]:
        (data_dir / name).write_text(f"FAKE_{name}")

    progress, admissions, preparations, pools = [], [], [], []

    class ObservedManager(WetlandsEnvManager):
        def admit_runtime(self, spec, **kwargs):
            callback = kwargs.pop("on_preparation", None)

            def observed(preparation):
                preparations.append((preparation, {event.node_name for event in progress if event.status == "started"}))
                if callback is not None:
                    callback(preparation)

            receipt = super().admit_runtime(spec, on_preparation=observed, **kwargs)
            admissions.append((kwargs["scoped_node_path"], receipt))
            return receipt

        def get_or_create(self, *args, **kwargs):
            pool = super().get_or_create(*args, **kwargs)
            pools.append(pool)
            return pool

    manager = ObservedManager(use_local_bioimageflow_core=False)
    engine = None
    try:
        with Workflow(
            storage_path=tmp_path / "results", engine="wetlands",
            max_workers=1, on_progress=progress.append,
        ) as wf:
            raw = FileLoader()(path=str(data_dir))
            out = SimpleRowTool()(input_path=raw["path"], name="prepared_worker")
            engine = wf.create_engine(env_manager=manager, resource_lifetime="external")
            df = wf.compute(out, engine=engine)

        assert len(df) == 3
        assert set(df["value"]) == {42.0}
        for output_path in df["output_path"]:
            path = Path(output_path)
            assert path.exists()
            assert path.read_text().startswith("processed:")
        assert [scope for scope, _ in admissions] == ["prepared_worker"]
        assert len(preparations) == 1
        preparation, started_before_preparation = preparations[0]
        assert preparation.scoped_node_path == "prepared_worker"
        assert "prepared_worker" not in started_before_preparation
        assert "prepared_worker" in {event.node_name for event in progress if event.status == "started"}
        assert len({id(pool) for pool in pools}) == 1
        pool = pools[0]
        assert pool.worker_count == 1

        # Only stdlib and installed Core are imported by this metadata probe.
        # The .tif inputs above are inert text fixtures, not image validation.
        probe = tmp_path / "installed_core_probe.py"
        probe.write_text('''def facts():
    import importlib.metadata
    import json
    import os
    from pathlib import Path
    import sys
    import bioimageflow_core
    distribution = importlib.metadata.distribution("bioimageflow-core")
    direct = distribution.read_text("direct_url.json")
    return {"version": distribution.version, "origin": str(Path(bioimageflow_core.__file__).resolve()),
            "prefix": str(Path(sys.prefix).resolve()), "pid": os.getpid(),
            "direct_url": json.loads(direct) if direct else None}
''')
        installed = pool.execute_path(probe, "facts", timeout=30)
        assert installed["pid"] != os.getpid()
        assert installed["version"] == version("bioimageflow-core")
        assert Path(installed["origin"]).is_relative_to(Path(installed["prefix"]))
        assert Path(installed["prefix"]).is_relative_to(pool.environment.path.resolve())
        if installed["direct_url"] is not None:
            assert not installed["direct_url"].get("dir_info")
            assert urlsplit(installed["direct_url"]["url"]).hostname == "files.pythonhosted.org"
        lock = yaml.safe_load(pool.environment.pixi_lock_path.read_text())
        records = [record for record in lock["packages"] if record.get("name") == "bioimageflow-core"]
        assert len(records) == 1
        record = records[0]
        artifact = urlsplit(record["pypi"])
        assert artifact.scheme == "https" and artifact.hostname == "files.pythonhosted.org"
        assert artifact.path.endswith(f"/bioimageflow_core-{installed['version']}-py3-none-any.whl")
        assert re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
        receipt = admissions[0][1]
        content = [item for item in receipt.to_scientific_facts()["distributions"] if item["name"] == "bioimageflow-core"]
        assert len(content) == 1
        (tmp_path / "published-core-proof.json").write_text(json.dumps({
            "installed_worker": installed,
            "resolved_artifact": {"url": record["pypi"], "sha256": record["sha256"]},
            "admitted_content": content[0], "generation_id": receipt.generation_id,
            "lockfile_hash": receipt.lockfile_hash,
            "qualifier": "Real WorkerPool; compare resolved wheel hash with the canonical published Core release artifact separately.",
        }, indent=2) + "\n")
    finally:
        try:
            if engine is not None:
                engine.close()
        finally:
            manager.shutdown_all()


def test_configured_logging_routes_wetlands_worker_streams(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert Version(version("wetlands")) >= Version("1.1.0")

    snapshot = _snapshot_loggers()
    try:
        configure_logging(fmt="%(levelname)s:%(message)s", force=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "img_01.tif").write_text("FAKE")

        with Workflow(
            storage_path=tmp_path / "results", engine="wetlands",
            max_workers=1,
        ) as wf:
            raw = FileLoader()(path=str(data_dir))
            out = WorkerStreamTool()(input_path=raw["path"])
            df = wf.compute(out)

        assert len(df) == 1
        captured = capsys.readouterr()
        assert "INFO:worker routine stdout" in captured.out
        assert "INFO:worker actual stderr" in captured.out
        assert captured.err == ""
    finally:
        _restore_loggers(snapshot)
