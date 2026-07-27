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


from tests.testkit.worker_timeout import (
    _StubTool,
)


class TestWorkflowEnvironmentField:
    def test_default_is_none(self):
        env = WorkflowEnvironment(name="test")
        assert env.worker_timeout is None

    def test_assignment(self):
        env = WorkflowEnvironment(name="test")
        env.worker_timeout = 120.0
        assert env.worker_timeout == 120.0

    def test_explicit_none(self):
        env = WorkflowEnvironment(name="test", worker_timeout=None)
        assert env.worker_timeout is None

    def test_via_get_environment(self, tmp_path):
        spec = EnvironmentSpec(name="test_env", dependencies={})
        wf = Workflow(storage_path=tmp_path, engine="direct")
        cfg = wf.get_environment(spec)
        assert cfg.worker_timeout is None
        cfg.worker_timeout = 30.0
        assert wf.get_environment(spec).worker_timeout == 30.0


class TestComputeEngineTimeout:
    def test_none_returns_none(self):
        assert _compute_engine_timeout(None) is None

    def test_small_timeout_uses_additive_margin(self):
        # 10s * 1.5 = 15s; 10 + 60 = 70s → max is 70
        assert _compute_engine_timeout(10.0) == 70.0

    def test_large_timeout_uses_multiplicative_margin(self):
        # 200 * 1.5 = 300; 200 + 60 = 260 → max is 300
        assert _compute_engine_timeout(200.0) == 300.0

    def test_boundary(self):
        # 120 * 1.5 = 180; 120 + 60 = 180 → equal
        assert _compute_engine_timeout(120.0) == 180.0


class TestResolveWorkerConfig:
    def test_default_engine_returns_none_when_not_configured(self, tmp_path):
        engine = DefaultEngine(use_wetlands=False)
        tool = _StubTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")
        mw, we, wt = engine._resolve_worker_config(tool, wf)
        assert wt is None

    def test_default_engine_returns_configured_timeout(self, tmp_path):
        engine = DefaultEngine(use_wetlands=False)
        tool = _StubTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")
        wf.get_environment(tool).worker_timeout = 45.0
        mw, we, wt = engine._resolve_worker_config(tool, wf)
        assert wt == 45.0

    def test_sequential_engine_respects_timeout(self, tmp_path):
        engine = SequentialEngine(use_wetlands=False)
        tool = _StubTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")
        wf.get_environment(tool).worker_timeout = 15.0
        mw, we, wt = engine._resolve_worker_config(tool, wf)
        assert mw == 1
        assert we is None
        assert wt == 15.0
