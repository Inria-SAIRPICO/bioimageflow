"""Helpers for running external commands from BioImageFlow tools."""

from __future__ import annotations

import os
import shlex
import signal
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
