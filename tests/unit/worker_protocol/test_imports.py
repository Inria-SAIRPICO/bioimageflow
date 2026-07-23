"""Worker protocol import and single-entry-point contracts."""

from __future__ import annotations

import inspect
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).parents[3]


def test_core_worker_modules_import_without_orchestrator_dependencies() -> None:
    code = textwrap.dedent(
        """
        import sys
        import bioimageflow_core.worker
        import bioimageflow_core.worker_origins
        import bioimageflow_core.worker_protocol

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
        """
    )
    subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_worker_exposes_only_the_canonical_processing_entry_point() -> None:
    import bioimageflow_core.worker as worker

    public_functions = {
        name
        for name, function in inspect.getmembers(worker, inspect.isfunction)
        if function.__module__ == worker.__name__ and not name.startswith("_")
    }
    assert public_functions == {"execute_processing_task"}
