"""Worker-safe executor preflight probe tests."""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from bioimageflow_core.preflight import (
    PREFLIGHT_SCHEMA,
    execute_executor_preflight,
)
from bioimageflow_core.worker_origins import (
    SourceFileOriginV1,
    encode_worker_tool_origin,
    worker_tool_origin_identity,
)


ROOT = Path(__file__).parents[3]
CURRENT_CORE_REQUIREMENT = (
    f"bioimageflow-core=={importlib.metadata.version('bioimageflow-core')}"
)


def _write_tool(source: Path, marker: Path) -> SourceFileOriginV1:
    source.write_text(
        f"""
from pathlib import Path
from bioimageflow_core import Arguments, IOModel, ProcessingTool

class ProbeTool(ProcessingTool):
    class Inputs(IOModel):
        value: str
    class Outputs(IOModel):
        value: str
    def __init__(self):
        Path({str(marker)!r}).write_text("constructed", encoding="utf-8")
    def process_row(self, arguments: Arguments):
        Path({str(marker)!r}).write_text("processed", encoding="utf-8")
        return self.Outputs(value=arguments.value)
""",
        encoding="utf-8",
    )
    return SourceFileOriginV1(
        path=str(source.resolve()),
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        class_name="ProbeTool",
    )


def _request(
    tmp_path: Path,
    *,
    origin: SourceFileOriginV1 | None = None,
    readable_paths: list[str] | None = None,
) -> dict[str, object]:
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    selected_origin = origin or _write_tool(
        tmp_path / "probe_tool.py",
        tmp_path / "tool_invoked",
    )
    paths = readable_paths or [
        str(Path(selected_origin.path).resolve()),
        str(storage.resolve()),
    ]
    encoded_origin = encode_worker_tool_origin(selected_origin)
    return {
        "schema": PREFLIGHT_SCHEMA,
        "executor_label": "cpu",
        "core_requirements": [CURRENT_CORE_REQUIREMENT],
        "storage_root": str(storage.resolve()),
        "sentinel_path": str(
            (storage / ".preflight" / "session" / "sentinel").resolve()
        ),
        "readable_paths": sorted(paths),
        "origins": [encoded_origin],
    }


def test_probe_returns_exact_success_evidence_without_invoking_tool(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    origin = SourceFileOriginV1(
        path=str((tmp_path / "probe_tool.py").resolve()),
        source_hash=hashlib.sha256(
            (tmp_path / "probe_tool.py").read_bytes()
        ).hexdigest(),
        class_name="ProbeTool",
    )

    result = execute_executor_preflight(request)

    assert result == {
        "schema": "bioimageflow.parsl.executor_preflight_result.v1",
        "executor_label": "cpu",
        "worker_api": "bioimageflow.processing_task.v1",
        "core_version": importlib.metadata.version("bioimageflow-core"),
        "core_requirements": [CURRENT_CORE_REQUIREMENT],
        "core_compatible": True,
        "storage_root": request["storage_root"],
        "sentinel_path": request["sentinel_path"],
        "sentinel_write": True,
        "sentinel_read": True,
        "sentinel_delete": True,
        "path_results": [
            {
                "path": path,
                "resolved_path": path,
                "readable": True,
            }
            for path in request["readable_paths"]
        ],
        "origin_results": [
            {
                "identity": worker_tool_origin_identity(origin),
                "kind": "source_file",
                "resolved": True,
            }
        ],
    }
    assert not (tmp_path / "tool_invoked").exists()
    assert not Path(request["sentinel_path"]).exists()
    assert not (tmp_path / "storage" / ".preflight").exists()


def test_probe_reports_path_and_origin_failures_and_cleans_sentinel(
    tmp_path: Path,
) -> None:
    source = tmp_path / "probe_tool.py"
    origin = _write_tool(source, tmp_path / "tool_invoked")
    mismatched = SourceFileOriginV1(
        path=origin.path,
        source_hash="0" * 64,
        class_name=origin.class_name,
    )
    missing = str((tmp_path / "missing").resolve())
    storage = str((tmp_path / "storage").resolve())
    request = _request(
        tmp_path,
        origin=mismatched,
        readable_paths=[missing, storage],
    )

    result = execute_executor_preflight(request)

    assert result["path_results"] == [
        {
            "path": missing,
            "resolved_path": missing,
            "readable": False,
        },
        {
            "path": storage,
            "resolved_path": storage,
            "readable": True,
        },
    ]
    assert result["origin_results"][0]["resolved"] is False
    assert result["sentinel_delete"] is True
    assert not Path(request["sentinel_path"]).exists()
    assert not (tmp_path / "tool_invoked").exists()


def test_probe_reports_incompatible_core_requirement(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["core_requirements"] = ["bioimageflow-core==999.0.0"]

    result = execute_executor_preflight(request)

    assert result["core_version"] == importlib.metadata.version(
        "bioimageflow-core"
    )
    assert result["core_compatible"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(
            schema="bioimageflow.parsl.executor_preflight.v2"
        ),
        lambda payload: payload.update(extra=True),
        lambda payload: payload.pop("executor_label"),
        lambda payload: payload.update(executor_label=" cpu"),
        lambda payload: payload.update(storage_root="relative"),
        lambda payload: payload.update(sentinel_path="/outside/sentinel"),
        lambda payload: payload.update(
            sentinel_path=str(
                Path(payload["storage_root"]) / "cache" / "preflight"
            )
        ),
        lambda payload: payload.update(
            core_requirements=[
                "bioimageflow-core>=0.1.7,<0.2",
                "bioimageflow-core>=0.1.7,<0.2",
            ]
        ),
        lambda payload: payload.update(core_requirements=[]),
        lambda payload: payload.update(
            core_requirements=["bioimageflow-core>=0.1.7;python_version>'3'"]
        ),
        lambda payload: payload.update(
            readable_paths=[
                payload["readable_paths"][0],
                payload["readable_paths"][0],
            ]
        ),
        lambda payload: payload.update(readable_paths=[]),
        lambda payload: payload.update(
            readable_paths=[
                path
                for path in payload["readable_paths"]
                if path != payload["storage_root"]
            ]
        ),
        lambda payload: payload.update(
            origins=[dict(payload["origins"][0], extra=True)]
        ),
        lambda payload: payload.update(
            origins=[payload["origins"][0], payload["origins"][0]]
        ),
        lambda payload: payload.update(origins=[]),
    ],
)
def test_probe_rejects_noncanonical_requests(
    tmp_path: Path,
    mutation,
) -> None:
    request = _request(tmp_path)
    mutation(request)

    with pytest.raises(ValueError):
        execute_executor_preflight(request)

    assert not Path(request["sentinel_path"]).exists()


def test_probe_requires_a_plain_request_object(tmp_path: Path) -> None:
    class Request(dict):
        pass

    with pytest.raises(ValueError, match="plain object"):
        execute_executor_preflight(Request(_request(tmp_path)))


def test_preflight_module_imports_without_orchestrator_dependencies() -> None:
    code = textwrap.dedent(
        """
        import inspect
        import sys
        import bioimageflow_core.preflight as preflight

        forbidden = [
            name
            for name in sys.modules
            if name == "bioimageflow"
            or name.startswith("bioimageflow.")
            or name == "pandas"
            or name.startswith("pandas.")
            or name == "parsl"
            or name.startswith("parsl.")
            or name == "pydantic"
            or name.startswith("pydantic.")
        ]
        if forbidden:
            raise SystemExit(f"forbidden imports: {forbidden}")
        public_functions = {
            name
            for name, function in inspect.getmembers(
                preflight, inspect.isfunction
            )
            if function.__module__ == preflight.__name__
            and not name.startswith("_")
        }
        if public_functions != {"execute_executor_preflight"}:
            raise SystemExit(f"public functions: {public_functions}")
        """
    )

    subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
