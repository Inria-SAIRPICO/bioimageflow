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
    """Verify the value flows from user code to Wetlands 2 pool startup."""

    def test_env_manager_forwards_worker_timeout_to_launch(self, monkeypatch):
        """The manager passes ``worker_timeout`` to ``environment.start``."""
        from bioimageflow.env_manager import WetlandsEnvManager

        # Patch the shared environment manager so get_or_create doesn't try
        # to actually create a conda env.
        launch_calls: list[dict] = []

        class _FakeEnv:
            def start(self, **kwargs):
                launch_calls.append(kwargs)
                return object()

        class _Operation:
            def wait_for(self):
                return _FakeEnv()

        class _FakeManager:
            def provision(self, name, spec):
                return _Operation()

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
        """Wetlands 2 receives its explicit default ``worker_timeout=None``."""
        from bioimageflow.env_manager import WetlandsEnvManager

        launch_calls: list[dict] = []

        class _FakeEnv:
            def start(self, **kwargs):
                launch_calls.append(kwargs)
                return object()

        class _Operation:
            def wait_for(self):
                return _FakeEnv()

        class _FakeManager:
            def provision(self, name, spec):
                return _Operation()

        monkeypatch.setattr(
            "bioimageflow.env_manager.get_shared_environment_manager",
            lambda **kw: _FakeManager(),
        )

        mgr = WetlandsEnvManager()
        spec = EnvironmentSpec(name="wt_plumb_none", dependencies={"pip": []})
        mgr.get_or_create(spec, max_workers=1, worker_timeout=None)

        assert len(launch_calls) == 1
        assert launch_calls[0]["worker_timeout"] is None
