"""Actual WorkerPool execution after an explained output collision."""

import os
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from bioimageflow import OutputPathCollisionError, WetlandsEnvManager, Workflow
from output_file_test_tools.source import ImageRows
from output_file_test_tools.processing import WorkerNumberFile


@contextmanager
def _core_dependency():
    wheel = os.environ.get("BIOIMAGEFLOW_TEST_CORE_WHEEL")
    if not wheel:
        yield {"bioimageflow_core_source": Path(__file__).resolve().parents[2] / "packages/bioimageflow-core"}
        return
    wheel_path = Path(wheel).resolve(strict=True)
    # Wetlands treats file URLs as source directories. Serve the unchanged wheel
    # through its ordinary URL dependency path to exercise artifact installation.
    handler = partial(SimpleHTTPRequestHandler, directory=str(wheel_path.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"bioimageflow_core_dependency": f"bioimageflow-core @ http://127.0.0.1:{server.server_port}/{wheel_path.name}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.wetlands
def test_worker_collision_correction_preserves_cache_and_writes_distinct_files(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOIMAGEFLOW_HOME", str(tmp_path / "home"))
    with _core_dependency() as dependency:
        _run_collision_correction(tmp_path, dependency)


def _run_collision_correction(tmp_path, dependency):
    manager = WetlandsEnvManager(root=tmp_path / "wetlands", **dependency)
    engine = None
    try:
        with Workflow(engine="wetlands", storage_path=tmp_path / "results", max_workers=1) as workflow:
            source = ImageRows()(paths=["/data/image.tif"] * 3, name="numbers")
            writer = WorkerNumberFile()(image=source["image"], threshold=source["threshold"], name="writer")
            engine = workflow.create_engine(env_manager=manager, resource_lifetime="external")
            with pytest.raises(OutputPathCollisionError) as error:
                workflow.compute(writer, engine=engine)
            assert not list((tmp_path / "results").rglob("result.txt"))
            writer.output_templates.update(error.value.report["suggested_templates"])
            result = workflow.compute(writer, engine=engine)
            assert len(set(result["report"])) == 3
            assert [Path(path).read_text() for path in result["report"]] == ["0", "2", "4"]
            assert all(pid != os.getpid() for pid in result["worker_pid"])
            assert len(set(result["worker_pid"])) == 1
    finally:
        if engine is not None:
            engine.close()
        manager.shutdown_all()
