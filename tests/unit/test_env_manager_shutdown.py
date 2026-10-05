"""Fault-injected public manager/engine composition, not worker/PID certification."""
from bioimageflow import DefaultEngine, ResourceLifetime
from bioimageflow_core import EnvironmentSpec
from bioimageflow import env_manager
from bioimageflow.env_manager import EnvironmentShutdownError
import pytest
from tests.testkit.env_manager_fakes import _GenerationManager


def _engine(monkeypatch, tmp_path):
    dependency = _GenerationManager()
    monkeypatch.setattr(env_manager, "get_shared_environment_manager", lambda **kwargs: dependency)
    engine = DefaultEngine(use_wetlands=True, resource_lifetime=ResourceLifetime.ENGINE,
        wetlands_config={"root": tmp_path / "managed", "bioimageflow_core_dependency": "bioimageflow-core==0.5.0"})
    manager = engine.environment_manager
    assert manager is not None
    pool = manager.get_or_create(EnvironmentSpec(name="selected", dependencies={"python": "3.12"}))
    return engine, manager, pool


def test_engine_close_retries_retained_manager_pool(monkeypatch, tmp_path):
    engine, manager, pool = _engine(monkeypatch, tmp_path)
    failure = RuntimeError("controlled physical pool close failure")
    pool.close_failure = failure
    first_error = None
    try:
        engine.close()
    except BaseException as error:
        first_error = error
    assert manager.is_running("selected"), "Failed pool must remain owned for retry"
    assert pool.close_count == 1
    pool.close_failure = None
    engine.close()
    observed = (first_error, pool.close_count, manager.running_environments())
    assert isinstance(first_error, EnvironmentShutdownError)
    assert first_error.failures == (("selected", failure),)
    assert first_error.__cause__ is failure
    assert pool.close_count == 2 and not manager.is_running("selected"), observed
    engine.close()
    assert pool.close_count == 2


def test_same_thread_close_listener_can_read_manager_state(monkeypatch, tmp_path):
    engine, manager, pool = _engine(monkeypatch, tmp_path)
    reads = []
    original_close = pool.close
    def listener_close():
        reads.append((manager.is_running("selected"), manager.running_environments()))
        original_close()
    pool.close = listener_close
    engine.close()
    assert reads == [(True, ("selected",))]
    assert pool.close_count == 1 and not manager.is_running("selected")
    engine.close()
    assert pool.close_count == 1


def test_shutdown_attempts_all_pools_and_retries_only_retained_failures(monkeypatch, tmp_path):
    engine, manager, first = _engine(monkeypatch, tmp_path)
    second = manager.get_or_create(EnvironmentSpec(name="second", dependencies={"python": "3.12"}))
    success = manager.get_or_create(EnvironmentSpec(name="success", dependencies={"python": "3.12"}))
    failures = (RuntimeError("selected close failure"), OSError("second close failure"))
    first.close_failure, second.close_failure = failures
    with pytest.raises(EnvironmentShutdownError) as caught:
        manager.shutdown_all()
    assert caught.value.failures == (("selected", failures[0]), ("second", failures[1]))
    assert caught.value.__cause__ is failures[0]
    assert manager.running_environments() == ("second", "selected")
    assert first.close_count == second.close_count == success.close_count == 1
    first.close_failure = second.close_failure = None
    manager.shutdown_all()
    assert manager.running_environments() == ()
    assert first.close_count == second.close_count == 2 and success.close_count == 1
    engine.close()


def test_best_effort_stop_preserves_selected_failure_and_other_pool(monkeypatch, tmp_path):
    engine, manager, pool = _engine(monkeypatch, tmp_path)
    other = manager.get_or_create(EnvironmentSpec(name="other", dependencies={"python": "3.12"}))
    pool.close_failure = RuntimeError("selected close failure")
    assert not manager.stop("selected")
    assert manager.running_environments() == ("other", "selected")
    assert other.close_count == 0
    pool.close_failure = None
    assert manager.stop("selected")
    assert not manager.stop("selected")
    assert pool.close_count == 2 and other.close_count == 0
    engine.close()
    assert other.close_count == 1


def test_shutdown_retries_grant_retirement_after_pool_is_physically_closed(monkeypatch, tmp_path):
    from types import SimpleNamespace
    engine, manager, pool = _engine(monkeypatch, tmp_path)
    other = manager.get_or_create(EnvironmentSpec(name="other", dependencies={"python": "3.12"}))
    physical_closes = []
    def close_once():
        if not physical_closes:
            physical_closes.append("closed")
    pool.close = close_once
    def submit_import(*args, **kwargs):
        return SimpleNamespace(state=SimpleNamespace(terminal=True))
    pool.submit_import = submit_import
    failure = RuntimeError("controlled grant retirement failure")
    class Grant:
        attempts = 0
        released = False
        def drained(self):
            self.attempts += 1
            if self.attempts == 1:
                raise failure
            self.released = True
    grant = Grant()
    manager.submit_processing_task(EnvironmentSpec(name="selected", dependencies={"python": "3.12"}),
                                   {}, shared_memory_grant=grant)
    with pytest.raises(EnvironmentShutdownError) as caught:
        manager.shutdown_all()
    assert caught.value.failures == (("selected", failure),) and caught.value.__cause__ is failure
    assert manager.running_environments() == ("selected",)
    assert not grant.released and grant.attempts == 1
    assert other.close_count == 1 and physical_closes == ["closed"]
    manager.shutdown_all()
    assert grant.released and grant.attempts == 2
    assert manager.running_environments() == () and physical_closes == ["closed"]
    engine.close()
