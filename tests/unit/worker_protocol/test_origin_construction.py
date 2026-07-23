"""Orchestrator construction of complete worker origins."""

from __future__ import annotations

import importlib.metadata

import pytest
from bioimageflow.worker_origins import resolve_worker_tool_origin
from bioimageflow_core import (
    InstalledModuleOriginV1,
    ProcessingTool,
    SharedModuleOriginV1,
)
from bioimageflow_core.worker_origins import load_worker_tool


def test_source_checkout_defaults_to_a_verified_shared_module() -> None:
    origin = resolve_worker_tool_origin(ProcessingTool)

    assert isinstance(origin, SharedModuleOriginV1)
    assert origin.module == "bioimageflow_core.tool"
    assert origin.class_name == "ProcessingTool"


def test_installed_module_requires_explicit_distribution_identity() -> None:
    try:
        version = importlib.metadata.version("bioimageflow-core")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("bioimageflow-core metadata is unavailable")

    origin = resolve_worker_tool_origin(
        ProcessingTool,
        installed_distribution="bioimageflow-core",
    )

    assert origin == InstalledModuleOriginV1(
        distribution="bioimageflow-core",
        version=version,
        module="bioimageflow_core.tool",
        class_name="ProcessingTool",
    )
    assert type(load_worker_tool(origin)) is ProcessingTool


def test_installed_distribution_spelling_must_be_canonical() -> None:
    with pytest.raises(ValueError, match="canonical normalized"):
        resolve_worker_tool_origin(
            ProcessingTool,
            installed_distribution="bioimageflow_core",
        )
