from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import bioimageflow.launcher.backends as backend_module
from bioimageflow.launcher.artifacts import read_manual_command
from bioimageflow.launcher.backends import (
    LocalLaunch,
    ManualLaunch,
    build_orchestrator_argv,
    launch_orchestrator,
)
from bioimageflow.launcher.control import LauncherRunControl
from bioimageflow.launcher.errors import (
    BackendNotSupportedError,
    LauncherProtocolError,
)
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.launcher.types import OrchestratorLaunchConfig
from tests.unit.launcher.helpers import launcher_submission


class _FakeProcess:
    pid = 4321


def _submission(
    storage_root: Path,
    run_id: str,
    *,
    backend: str,
) -> dict[str, object]:
    return launcher_submission(storage_root, run_id, backend=backend)


def _control(tmp_path: Path, *, backend: str) -> LauncherRunControl:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    return repository.allocate(
        _submission(repository.storage_root, run_id, backend=backend),
        backend=backend,
    )


def test_orchestrator_argv_has_only_explicit_storage_and_run_identity(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path, backend="local")

    argv = build_orchestrator_argv(control)

    assert argv == (
        str(Path(backend_module.sys.executable).absolute()),
        "-m",
        "bioimageflow.launcher.orchestrator",
        "--storage-root",
        str(tmp_path.resolve()),
        "--run-id",
        control.run_id,
    )


def test_local_backend_starts_detached_process_with_confined_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, backend="local")
    work_dir = tmp_path / "orchestrator-work"
    work_dir.mkdir()
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def fake_popen(
        argv: tuple[str, ...],
        **kwargs: Any,
    ) -> _FakeProcess:
        calls.append((argv, kwargs))
        kwargs["stdout"].write(b"stdout")
        kwargs["stderr"].write(b"stderr")
        return _FakeProcess()

    monkeypatch.setattr(backend_module.subprocess, "Popen", fake_popen)

    result = launch_orchestrator(
        control,
        OrchestratorLaunchConfig(backend="local", work_dir=work_dir),
        secret_refs=("BIF_API_TOKEN",),
    )

    assert isinstance(result, LocalLaunch)
    assert result.pid == 4321
    assert result.process.pid == 4321
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == result.argv
    assert kwargs["cwd"] == work_dir.resolve()
    assert kwargs["stdin"] is backend_module.subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert work_dir.as_posix() not in argv
    assert "BIF_API_TOKEN" not in argv
    assert result.stdout_path.read_bytes() == b"stdout"
    assert result.stderr_path.read_bytes() == b"stderr"
    assert result.stdout_path.parent == control.control_dir / "logs"
    assert result.stderr_path.parent == control.control_dir / "logs"
    assert not (control.control_dir / "command.json").exists()
    assert control.read_status()["state"] == "prepared"


def test_local_backend_never_persists_inherited_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, backend="local")
    monkeypatch.setenv("BIF_API_TOKEN", "literal-super-secret")
    monkeypatch.setattr(
        backend_module.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(),
    )

    result = launch_orchestrator(
        control,
        OrchestratorLaunchConfig(backend="local"),
        secret_refs=("BIF_API_TOKEN",),
    )

    assert isinstance(result, LocalLaunch)
    for path in control.control_dir.rglob("*"):
        if path.is_file():
            assert b"literal-super-secret" not in path.read_bytes()
    assert "literal-super-secret" not in result.argv


def test_local_backend_cleans_logs_when_process_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, backend="local")

    def fail_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        raise OSError("process unavailable")

    monkeypatch.setattr(backend_module.subprocess, "Popen", fail_popen)

    with pytest.raises(LauncherProtocolError, match="Could not start"):
        launch_orchestrator(
            control,
            OrchestratorLaunchConfig(backend="local"),
        )

    assert not (control.control_dir / "logs/orchestrator.out").exists()
    assert not (control.control_dir / "logs/orchestrator.err").exists()
    assert control.read_status()["state"] == "prepared"


def test_local_backend_requires_existing_non_symlink_work_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, backend="local")
    invoked = False

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        nonlocal invoked
        invoked = True
        return _FakeProcess()

    monkeypatch.setattr(backend_module.subprocess, "Popen", fake_popen)

    with pytest.raises(LauncherProtocolError, match="does not exist"):
        launch_orchestrator(
            control,
            OrchestratorLaunchConfig(
                backend="local",
                work_dir=tmp_path / "missing",
            ),
        )

    assert invoked is False
    assert not (control.control_dir / "logs").exists()


def test_manual_backend_persists_shell_free_descriptor_and_stays_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, backend="manual")
    work_dir = tmp_path / "manual-work"

    def forbidden_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        raise AssertionError("manual backend must not start a process")

    monkeypatch.setattr(backend_module.subprocess, "Popen", forbidden_popen)

    result = launch_orchestrator(
        control,
        OrchestratorLaunchConfig(backend="manual", work_dir=work_dir),
        secret_refs=("BIF_API_TOKEN", "BIF_DB_PASSWORD"),
    )

    assert isinstance(result, ManualLaunch)
    assert result.argv == build_orchestrator_argv(control)
    assert work_dir.resolve().as_posix() not in result.argv
    assert "BIF_API_TOKEN" not in result.argv
    descriptor = read_manual_command(control)
    assert descriptor == result.descriptor
    assert descriptor == {
        "schema": "bioimageflow.launcher.command.v1",
        "run_id": control.run_id,
        "argv": list(result.argv),
        "work_dir": str(work_dir.resolve()),
        "secret_refs": ["BIF_API_TOKEN", "BIF_DB_PASSWORD"],
    }
    assert control.read_status()["state"] == "prepared"
    assert not (control.control_dir / "logs").exists()
    assert "literal" not in json.dumps(descriptor)


@pytest.mark.parametrize("backend", ["slurm", "pbs", "lsf", "oar"])
def test_unsupported_backends_fail_before_process_or_artifact_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    control = _control(tmp_path, backend=backend)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unsupported backend performed an action")

    monkeypatch.setattr(backend_module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(backend_module, "write_manual_command", forbidden)

    with pytest.raises(BackendNotSupportedError) as error:
        launch_orchestrator(
            control,
            OrchestratorLaunchConfig(backend=backend),  # type: ignore[arg-type]
        )

    assert error.value.code == "backend-not-supported"
    assert error.value.details == {"backend": backend}
    assert list(control.control_dir.iterdir())
    assert not (control.control_dir / "logs").exists()
    assert not (control.control_dir / "command.json").exists()
    assert control.read_status()["state"] == "prepared"


def test_backend_must_match_allocated_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, backend="manual")
    invoked = False

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        nonlocal invoked
        invoked = True
        return _FakeProcess()

    monkeypatch.setattr(backend_module.subprocess, "Popen", fake_popen)

    with pytest.raises(LauncherProtocolError, match="does not match"):
        launch_orchestrator(
            control,
            OrchestratorLaunchConfig(backend="local"),
        )

    assert invoked is False
    assert not (control.control_dir / "logs").exists()


@pytest.mark.parametrize(
    "secret_refs",
    [
        "BIF_SECRET",
        ("",),
        (" BIF_SECRET",),
        ("BIF_SECRET", "BIF_SECRET"),
    ],
)
def test_secret_reference_names_are_strict(
    tmp_path: Path,
    secret_refs: object,
) -> None:
    control = _control(tmp_path, backend="manual")

    with pytest.raises((TypeError, ValueError)):
        launch_orchestrator(
            control,
            OrchestratorLaunchConfig(backend="manual"),
            secret_refs=secret_refs,  # type: ignore[arg-type]
        )

    assert not (control.control_dir / "command.json").exists()
