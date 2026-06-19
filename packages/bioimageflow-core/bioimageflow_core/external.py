"""Helpers for running external commands from BioImageFlow tools."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional, Union


class ExternalCommandError(RuntimeError):
    """Raised when a tool-owned external command fails.

    The error message is intentionally self-contained because these exceptions
    often cross worker-process boundaries before a user sees them.
    """

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] = (),
        returncode: Optional[int] = None,
        signal_name: Optional[str] = None,
        cwd: Optional[str] = None,
        context: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.returncode = returncode
        self.signal_name = signal_name
        self.cwd = cwd
        self.context = context
        self.stdout = stdout
        self.stderr = stderr


def _format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _run_subprocess(command: Sequence[str], run_kwargs: dict[str, Any]) -> Any:
    import subprocess

    return subprocess.run(command, **run_kwargs)


def _signal_name(returncode: int) -> Optional[str]:
    if returncode >= 0:
        return None
    signal_number = -returncode
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"signal {signal_number}"


def _format_output(name: str, value: Any) -> Optional[str]:
    if value in (None, b"", ""):
        return None
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return None
    return f"{name}:\n{text}"


def _build_command_error(
    *,
    command: Sequence[str],
    cwd: Optional[str],
    context: Optional[str],
    cause: BaseException,
    returncode: Optional[int] = None,
    stdout: Any = None,
    stderr: Any = None,
) -> ExternalCommandError:
    command_text = _format_command(command)
    lines: list[str] = []
    prefix = "External command failed"
    if context:
        prefix += f" while running {context}"
    lines.append(f"{prefix}: {command_text}")

    signal_name = _signal_name(returncode) if returncode is not None else None
    if signal_name is not None and returncode is not None:
        lines.append(f"Process terminated by signal {signal_name} ({-returncode}).")
    elif returncode is not None:
        lines.append(f"Process exited with status {returncode}.")
    else:
        launch_prefix = "Unable to start external command"
        if context:
            launch_prefix += f" while running {context}"
        lines[0] = f"{launch_prefix}: {command_text}"

    if cwd is not None:
        lines.append(f"Working directory: {cwd}")
    if returncode is None:
        lines.append(str(cause))

    stdout_text = _format_output("stdout", stdout)
    stderr_text = _format_output("stderr", stderr)
    if stdout_text is not None:
        lines.append(stdout_text)
    if stderr_text is not None:
        lines.append(stderr_text)

    return ExternalCommandError(
        "\n".join(lines),
        command=command,
        returncode=returncode,
        signal_name=signal_name,
        cwd=cwd,
        context=context,
        stdout=None if stdout_text is None else str(stdout),
        stderr=None if stderr_text is None else str(stderr),
    )


def run_external_command(
    command: Sequence[Any],
    *,
    cwd: Optional[Union[str, os.PathLike[str]]] = None,
    env: Optional[Mapping[str, str]] = None,
    context: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Run an external command and raise a detailed, picklable failure.

    Parameters are forwarded to :func:`subprocess.run`. The command values are
    stringified before execution so tools can pass ``Path`` and numeric options.
    """

    command_values = [str(value) for value in command]
    cwd_text = None if cwd is None else str(Path(cwd))
    run_kwargs = {"check": True, **kwargs}
    if cwd is not None:
        run_kwargs["cwd"] = cwd
    if env is not None:
        run_kwargs["env"] = env
    try:
        return _run_subprocess(command_values, run_kwargs)
    except Exception as exc:
        import subprocess

        if not isinstance(exc, subprocess.CalledProcessError):
            if isinstance(exc, OSError):
                error = _build_command_error(
                    command=command_values,
                    cwd=cwd_text,
                    context=context,
                    cause=exc,
                )
                raise error from exc
            raise

        error = _build_command_error(
            command=command_values,
            cwd=cwd_text,
            context=context,
            cause=exc,
            returncode=exc.returncode,
            stdout=getattr(exc, "stdout", None) or getattr(exc, "output", None),
            stderr=exc.stderr,
        )
        raise error from exc


def _default_staging_parent() -> Path:
    configured = os.environ.get("BIOIMAGEFLOW_EXTERNAL_STAGING_DIR")
    if configured:
        return Path(configured)
    tmp_root = Path("/tmp")
    if os.name != "nt" and tmp_root.is_dir() and os.access(tmp_root, os.W_OK):
        return tmp_root
    return Path(tempfile.gettempdir())


def _replace_file_from_staged_output(staged_output: Path, final_output: Path) -> None:
    if staged_output.is_dir():
        raise IsADirectoryError(
            "External command staged output is a directory; "
            "run_external_command_with_staged_output only supports single-file outputs: "
            f"{staged_output}"
        )
    if not staged_output.exists():
        raise FileNotFoundError(
            "External command completed but did not create staged output: "
            f"{staged_output}. Intended final output: {final_output}"
        )

    final_output.parent.mkdir(parents=True, exist_ok=True)
    final_temp = final_output.with_name(f".{final_output.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(staged_output, final_temp)
        os.replace(final_temp, final_output)
    finally:
        if final_temp.exists():
            final_temp.unlink()


def run_external_command_with_staged_output(
    command: Sequence[Any],
    *,
    output_path: Union[str, os.PathLike[str]],
    staging_parent: Optional[Union[str, os.PathLike[str]]] = None,
    cwd: Optional[Union[str, os.PathLike[str]]] = None,
    env: Optional[Mapping[str, str]] = None,
    context: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Run a command with one file output redirected through a short path.

    Some external native tools fail when asked to write directly to long or
    symlink-expanded paths. This helper replaces the requested output path in
    ``command`` with a short temporary file, runs the command, then copies the
    produced file back to the requested final path.
    """

    final_output = Path(output_path)
    final_output_text = str(final_output)
    parent = Path(staging_parent) if staging_parent is not None else _default_staging_parent()
    parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bif-external-", dir=parent) as temp_dir:
        staged_output = Path(temp_dir) / final_output.name
        staged_command: list[Any] = []
        replaced = False
        for value in command:
            if str(value) == final_output_text:
                staged_command.append(staged_output)
                replaced = True
            else:
                staged_command.append(value)

        if not replaced:
            raise ValueError(
                "Cannot stage external command output because the requested "
                f"output path is not present in the command: {final_output}"
            )

        result = run_external_command(
            staged_command,
            cwd=cwd,
            env=env,
            context=context,
            **kwargs,
        )
        _replace_file_from_staged_output(staged_output, final_output)
        return result
