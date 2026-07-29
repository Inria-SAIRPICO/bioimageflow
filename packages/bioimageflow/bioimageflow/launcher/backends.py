"""Shell-free adapters for starting a submitted workflow orchestrator."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from .artifacts import (
    build_error_payload,
    read_local_process_identity,
    write_local_process_exit,
    write_local_process_identity,
    write_manual_command,
)
from .errors import LauncherProtocolError
from .control import LauncherRunControl
from .types import LaunchConfig, OrchestratorLaunchConfig, PSIJLaunchConfig


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


class BackendLaunch(Protocol):
    """Common observable result of every launcher adapter."""

    @property
    def backend(self) -> str: ...


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


def launcher_log_paths(
    control: LauncherRunControl,
) -> tuple[Path, Path]:
    """Prepare and return the confined orchestrator stdout/stderr paths."""
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


def _terminate_process_handle(
    process: subprocess.Popen[bytes],
    *,
    timeout: float = 5.0,
) -> None:
    """Stop and reap a newly spawned process before it becomes reconnectable."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait(timeout=timeout)
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=timeout)


def _process_start_token(pid: int) -> str | None:
    """Return an OS process-birth token suitable for PID-reuse checks."""
    proc_stat = Path("/proc") / str(pid) / "stat"
    try:
        encoded = proc_stat.read_text()
    except OSError:
        pass
    else:
        closing = encoded.rfind(")")
        fields = encoded[closing + 2 :].split()
        if closing >= 0 and len(fields) > 19:
            if fields[0] == "Z":
                return None
            return f"proc:{fields[19]}"
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=,stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return None
    start, separator, process_state = output.rpartition(" ")
    if not separator or process_state.startswith("Z"):
        return None
    return f"ps:{start.strip()}"


def _reconcile_local_exit(
    control: LauncherRunControl,
    *,
    returncode: int,
) -> None:
    status = control.read_status()
    if status["state"] in {"succeeded", "failed", "cancelled", "lost"}:
        return
    if status["state"] == "prepared":
        error = LauncherProtocolError(
            "The local orchestrator exited before acquiring its execution claim.",
            details={"run_id": control.run_id, "returncode": returncode},
        )
        control.commit_terminal(
            expected_revision=status["revision"],
            expected_claim_epoch=None,
            new_state="failed",
            error_payload=build_error_payload(
                control.run_id,
                code="orchestrator-exited-before-claim",
                error=error,
                backend={"name": "local", "returncode": returncode},
            ),
        )
        return
    claim = control.read_claim()
    if claim is None:
        return
    from .schemas import parse_utc_timestamp

    remaining = (
        parse_utc_timestamp(claim["expires_at"], field="expires_at").timestamp()
        - time.time()
    )
    if remaining > 0:
        threading.Event().wait(remaining + 0.01)
    status = control.read_status()
    if status["state"] in {"succeeded", "failed", "cancelled", "lost"}:
        return
    try:
        from .orchestrator import run_orchestrator

        run_orchestrator(
            control.repository.storage_root,
            control.run_id,
            recover=True,
            backend_absent_confirmed=True,
        )
    except BaseException:
        return


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
            returncode = process.wait()
            try:
                write_local_process_exit(
                    control,
                    returncode=returncode,
                )
            except BaseException:
                pass
            try:
                _reconcile_local_exit(
                    control,
                    returncode=returncode,
                )
            except BaseException:
                pass
        finally:
            with _LOCAL_PROCESS_LOCK:
                if _LOCAL_PROCESSES.get(key) is process:
                    _LOCAL_PROCESSES.pop(key, None)

    reaper = threading.Thread(
        target=reap,
        name=f"bioimageflow-reaper-{control.run_id}",
        daemon=True,
    )
    try:
        reaper.start()
    except BaseException:
        with _LOCAL_PROCESS_LOCK:
            if _LOCAL_PROCESSES.get(key) is process:
                _LOCAL_PROCESSES.pop(key, None)
        raise


def terminate_local_orchestrator(
    control: LauncherRunControl,
    *,
    timeout: float = 5.0,
) -> bool:
    """Terminate a local orchestrator and confirm that exact process exited."""
    with _LOCAL_PROCESS_LOCK:
        process = _LOCAL_PROCESSES.get(_local_process_key(control))
    if process is not None and process.poll() is None:
        process.terminate()
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=timeout)
            except TypeError:
                wait()
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    return False
        else:
            deadline = time.monotonic() + timeout
            while process.poll() is None and time.monotonic() < deadline:
                threading.Event().wait(0.01)
        return process.poll() is not None
    try:
        identity = read_local_process_identity(control)
    except LauncherProtocolError:
        return False
    pid = identity["pid"]
    if _process_start_token(pid) != identity["start_token"]:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_start_token(pid) != identity["start_token"]:
            return True
        threading.Event().wait(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_start_token(pid) != identity["start_token"]:
            return True
        threading.Event().wait(0.02)
    return False


def _launch_local(
    control: LauncherRunControl,
    launch: OrchestratorLaunchConfig,
    argv: tuple[str, ...],
) -> LocalLaunch:
    work_dir = _validate_local_work_dir(launch.work_dir)
    stdout_path, stderr_path = launcher_log_paths(control)
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
    identity_written = False
    try:
        start_token = _process_start_token(process.pid)
        if start_token is None:
            raise LauncherProtocolError(
                "Could not persist a reconnectable local orchestrator identity.",
                details={"run_id": control.run_id},
            )
        _track_local_process(control, process)
        write_local_process_identity(
            control,
            pid=process.pid,
            start_token=start_token,
        )
        identity_written = True
    except BaseException:
        _terminate_process_handle(process)
        if identity_written and process.returncode is not None:
            try:
                write_local_process_exit(
                    control,
                    returncode=process.returncode,
                )
            except BaseException:
                pass
        raise
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
    launch: LaunchConfig,
    *,
    secret_refs: Sequence[str] = (),
) -> BackendLaunch:
    """Start or describe an orchestrator without changing prepared state."""
    normalized = launch.normalized()
    refs = _normalized_secret_refs(secret_refs)
    _validate_prepared_control(control, backend=normalized.backend)
    if isinstance(normalized, PSIJLaunchConfig):
        from .psij import launch_psij

        return launch_psij(control, normalized)
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
