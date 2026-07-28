"""Shell-free adapters for starting a submitted workflow orchestrator."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .artifacts import write_manual_command
from .errors import BackendNotSupportedError, LauncherProtocolError
from .control import LauncherRunControl
from .types import OrchestratorLaunchConfig


_UNIMPLEMENTED_BACKENDS = frozenset({"slurm", "pbs", "lsf", "oar"})
_LOCAL_PROCESS_LOCK = threading.RLock()
_LOCAL_PROCESSES: dict[tuple[str, str], subprocess.Popen[bytes]] = {}


@dataclass(frozen=True, slots=True)
class LocalLaunch:
    """A separately running local orchestrator process."""

    argv: tuple[str, ...]
    process: subprocess.Popen[bytes] = field(repr=False, compare=False)
    work_dir: Path | None
    stdout_path: Path
    stderr_path: Path
    backend: Literal["local"] = "local"

    @property
    def pid(self) -> int:
        return self.process.pid


@dataclass(frozen=True, slots=True)
class ManualLaunch:
    """A durable command descriptor awaiting an external launcher."""

    argv: tuple[str, ...]
    descriptor: Mapping[str, Any]
    work_dir: Path | None
    backend: Literal["manual"] = "manual"


BackendLaunch = LocalLaunch | ManualLaunch


def build_orchestrator_argv(control: LauncherRunControl) -> tuple[str, ...]:
    """Build the one supported shell-free orchestrator command."""
    storage_root = control.repository.storage_root
    if not storage_root.is_absolute():
        raise LauncherProtocolError(
            "Launcher storage root must be absolute before backend launch."
        )
    executable = Path(sys.executable).expanduser()
    if not executable.is_absolute():
        executable = Path.cwd() / executable
    return (
        str(executable),
        "-m",
        "bioimageflow.launcher.orchestrator",
        "--storage-root",
        str(storage_root),
        "--run-id",
        control.run_id,
    )


def _normalized_secret_refs(secret_refs: Sequence[str]) -> tuple[str, ...]:
    if isinstance(secret_refs, (str, bytes)) or not isinstance(secret_refs, Sequence):
        raise TypeError("secret_refs must be a sequence of opaque names.")
    normalized: list[str] = []
    seen: set[str] = set()
    for reference in secret_refs:
        if (
            type(reference) is not str
            or not reference
            or reference != reference.strip()
        ):
            raise ValueError("secret_refs must contain non-empty trimmed opaque names.")
        if reference in seen:
            raise ValueError("secret_refs must not contain duplicate names.")
        seen.add(reference)
        normalized.append(reference)
    return tuple(normalized)


def _validate_prepared_control(
    control: LauncherRunControl,
    *,
    backend: str,
) -> None:
    status = control.read_status()
    if status["state"] != "prepared":
        raise LauncherProtocolError(
            "An orchestrator backend may only launch a prepared run.",
            details={"run_id": control.run_id, "state": status["state"]},
        )
    if status["backend"] != backend:
        raise LauncherProtocolError(
            "Launcher backend does not match the allocated run.",
            details={
                "configured_backend": backend,
                "persisted_backend": status["backend"],
                "run_id": control.run_id,
            },
        )


def _validate_local_work_dir(work_dir: Path | None) -> Path | None:
    if work_dir is None:
        return None
    try:
        mode = work_dir.lstat().st_mode
    except FileNotFoundError as exc:
        raise LauncherProtocolError(
            f"Local orchestrator work directory does not exist: {work_dir}."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise LauncherProtocolError(
            "Local orchestrator work directory must be a non-symlink directory."
        )
    return work_dir


def _logs(
    control: LauncherRunControl,
) -> tuple[Path, Path]:
    logs_dir = control.confined_path("logs")
    try:
        logs_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        mode = logs_dir.lstat().st_mode
    except FileNotFoundError as exc:
        raise LauncherProtocolError(
            "Launcher log directory disappeared during local launch."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise LauncherProtocolError(
            "Launcher log directory must be a confined non-symlink directory."
        )
    stdout_path = control.confined_path("logs/orchestrator.out")
    stderr_path = control.confined_path("logs/orchestrator.err")
    return stdout_path, stderr_path


def _open_exclusive_log(path: Path) -> Any:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise LauncherProtocolError(
            f"Launcher log already exists: {path.name}."
        ) from exc
    except OSError as exc:
        raise LauncherProtocolError(
            f"Could not create launcher log: {path.name}."
        ) from exc
    return os.fdopen(descriptor, "wb")


def _local_process_key(
    control: LauncherRunControl,
) -> tuple[str, str]:
    return str(control.repository.storage_root), control.run_id


def _track_local_process(
    control: LauncherRunControl,
    process: subprocess.Popen[bytes],
) -> None:
    key = _local_process_key(control)
    with _LOCAL_PROCESS_LOCK:
        _LOCAL_PROCESSES[key] = process
    if not callable(getattr(process, "wait", None)):
        return

    def reap() -> None:
        try:
            process.wait()
        finally:
            with _LOCAL_PROCESS_LOCK:
                if _LOCAL_PROCESSES.get(key) is process:
                    _LOCAL_PROCESSES.pop(key, None)

    threading.Thread(
        target=reap,
        name=f"bioimageflow-reaper-{control.run_id}",
        daemon=True,
    ).start()


def terminate_local_orchestrator(control: LauncherRunControl) -> bool:
    """Terminate a local orchestrator tracked by this launcher process."""
    with _LOCAL_PROCESS_LOCK:
        process = _LOCAL_PROCESSES.get(_local_process_key(control))
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    return True


def _launch_local(
    control: LauncherRunControl,
    launch: OrchestratorLaunchConfig,
    argv: tuple[str, ...],
) -> LocalLaunch:
    work_dir = _validate_local_work_dir(launch.work_dir)
    stdout_path, stderr_path = _logs(control)
    stdout = _open_exclusive_log(stdout_path)
    try:
        stderr = _open_exclusive_log(stderr_path)
    except BaseException:
        stdout.close()
        stdout_path.unlink(missing_ok=True)
        raise
    try:
        process = subprocess.Popen(
            argv,
            cwd=work_dir,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            shell=False,
            start_new_session=True,
        )
    except Exception as exc:
        stdout.close()
        stderr.close()
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        raise LauncherProtocolError(
            "Could not start the local orchestrator process.",
            details={"run_id": control.run_id},
        ) from exc
    except BaseException:
        stdout.close()
        stderr.close()
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        raise
    finally:
        stdout.close()
        stderr.close()
    _track_local_process(control, process)
    return LocalLaunch(
        argv=argv,
        process=process,
        work_dir=work_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _launch_manual(
    control: LauncherRunControl,
    launch: OrchestratorLaunchConfig,
    argv: tuple[str, ...],
    *,
    secret_refs: tuple[str, ...],
) -> ManualLaunch:
    descriptor = write_manual_command(
        control,
        argv=argv,
        work_dir=launch.work_dir,
        secret_refs=secret_refs,
    )
    return ManualLaunch(
        argv=argv,
        descriptor=descriptor,
        work_dir=launch.work_dir,
    )


def launch_orchestrator(
    control: LauncherRunControl,
    launch: OrchestratorLaunchConfig,
    *,
    secret_refs: Sequence[str] = (),
) -> BackendLaunch:
    """Start or describe an orchestrator without changing prepared state."""
    if launch.backend in _UNIMPLEMENTED_BACKENDS:
        raise BackendNotSupportedError(
            f"Launcher backend {launch.backend!r} is not implemented.",
            details={"backend": launch.backend},
        )
    normalized = launch.normalized()
    refs = _normalized_secret_refs(secret_refs)
    _validate_prepared_control(control, backend=normalized.backend)
    argv = build_orchestrator_argv(control)
    if normalized.backend == "local":
        return _launch_local(control, normalized, argv)
    if normalized.backend == "manual":
        return _launch_manual(
            control,
            normalized,
            argv,
            secret_refs=refs,
        )
    raise AssertionError(f"Unhandled launcher backend: {normalized.backend}")
