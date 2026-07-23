"""Focused tests split from ``tests/unit/test_worker_timeout.py``."""

from __future__ import annotations


# ruff: noqa: F401

import pickle

from dataclasses import replace

from pathlib import Path

from typing import Any

import pytest

from bioimageflow.engine import (
    DefaultEngine,
    SequentialEngine,
    WorkerTaskError,
    WorkerTimeoutError,
    _compute_engine_timeout,
)

from bioimageflow.workflow import Workflow, WorkflowEnvironment

from bioimageflow_core import EnvironmentSpec, ExecutionContext, IOModel, ProcessingTool


class TestWorkerTimeoutPlumbing:
    """Verify the value flows from user code → env_manager.launch()."""

    def test_env_manager_forwards_worker_timeout_to_launch(self, monkeypatch):
        """``WetlandsEnvManager.get_or_create`` passes ``worker_timeout`` to
        ``env.launch(...)``."""
        from bioimageflow.env_manager import WetlandsEnvManager

        # Patch the shared environment manager so get_or_create doesn't try
        # to actually create a conda env.
        launch_calls: list[dict] = []

        class _FakeEnv:
            def launch(self, **kwargs):
                launch_calls.append(kwargs)

        class _FakeManager:
            def create(self, name, deps):
                return _FakeEnv()

        monkeypatch.setattr(
            "bioimageflow.env_manager.get_shared_environment_manager",
            lambda **kw: _FakeManager(),
        )

        mgr = WetlandsEnvManager()
        spec = EnvironmentSpec(name="wt_plumb", dependencies={"pip": []})
        mgr.get_or_create(spec, max_workers=1, worker_timeout=17.0)

        assert len(launch_calls) == 1
        assert launch_calls[0].get("worker_timeout") == 17.0

    def test_env_manager_omits_worker_timeout_when_none(self, monkeypatch):
        """When ``worker_timeout=None``, do not pass the kwarg at all.

        This keeps the manager compatible with Wetlands versions that do not
        accept the keyword.
        """
        from bioimageflow.env_manager import WetlandsEnvManager

        launch_calls: list[dict] = []

        class _FakeEnv:
            def launch(self, **kwargs):
                launch_calls.append(kwargs)

        class _FakeManager:
            def create(self, name, deps):
                return _FakeEnv()

        monkeypatch.setattr(
            "bioimageflow.env_manager.get_shared_environment_manager",
            lambda **kw: _FakeManager(),
        )

        mgr = WetlandsEnvManager()
        spec = EnvironmentSpec(name="wt_plumb_none", dependencies={"pip": []})
        mgr.get_or_create(spec, max_workers=1, worker_timeout=None)

        assert len(launch_calls) == 1
        assert "worker_timeout" not in launch_calls[0]
