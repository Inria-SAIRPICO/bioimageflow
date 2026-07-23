"""Optional dependency and public import contracts for Parsl."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow.parsl import optional_dependency


ROOT = Path(__file__).parents[3]


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="cpu",
        environments=(
            WorkerEnvironmentAttestation(
                name="analysis",
                dependency_hash="b" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.1.7,<0.2",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=2),
        ),
    )


def test_public_import_and_engine_construction_do_not_import_external_parsl() -> None:
    code = """
import importlib.abc
import sys

class BlockParsl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "parsl" or fullname.startswith("parsl."):
            raise AssertionError(f"unexpected external import: {fullname}")
        return None

sys.meta_path.insert(0, BlockParsl())

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)

binding = ExecutorBinding(
    label="cpu",
    environments=(
        WorkerEnvironmentAttestation(
            name="analysis",
            dependency_hash="c" * 64,
            allow_flexible_versions=False,
            core_requirement="bioimageflow-core>=0.1.7,<0.2",
        ),
    ),
    capabilities=ExecutorCapabilities(
        storage_modes=("shared_fs",),
        tool_origin_modes=("installed_module",),
        slot=WorkerSlotCapacity(cpu=1),
    ),
)
ParslEngine(parsl_config=object(), executor_bindings={"cpu": binding})
assert "parsl" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_dependency_error_names_package_and_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str):
        assert name == "parsl"
        raise ModuleNotFoundError("No module named 'parsl'", name="parsl")

    monkeypatch.setattr(optional_dependency, "import_module", missing)
    engine = ParslEngine(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
    )

    with pytest.raises(ImportError) as exc_info:
        engine.execute([], object())

    message = str(exc_info.value)
    assert "parsl" in message
    assert "bioimageflow[parsl]" in message


def test_transitive_import_error_is_not_misreported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_dependency(name: str):
        assert name == "parsl"
        raise ModuleNotFoundError("No module named 'missing_transitive'", name="missing_transitive")

    monkeypatch.setattr(optional_dependency, "import_module", broken_dependency)

    with pytest.raises(ModuleNotFoundError, match="missing_transitive"):
        optional_dependency.require_parsl()


def test_external_parsl_is_loaded_only_through_focused_boundary() -> None:
    source = importlib.import_module("bioimageflow.parsl.optional_dependency")

    assert callable(source.require_parsl)
