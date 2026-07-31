from __future__ import annotations

import subprocess

import pytest

from pathlib import Path

from bioimageflow_core import (
    ExternalCommandError,
    run_external_command,
    run_external_command_with_staged_output,
)
from bioimageflow_core import external


def test_subprocess_resolves_cli_next_to_environment_python(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "environment-tool"
    executable.write_text("")
    calls: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(external.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(external.shutil, "which", lambda _name: None)
    monkeypatch.setattr(external.subprocess, "run", fake_subprocess_run)

    external._run_subprocess(["environment-tool", "--version"], {})

    assert calls == [[str(executable), "--version"]]


def test_run_external_command_reports_signal_failures(monkeypatch) -> None:
    def fake_run(command, run_kwargs):
        raise subprocess.CalledProcessError(
            returncode=-5,
            cmd=command,
            stderr="native crash details",
        )

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    with pytest.raises(ExternalCommandError) as exc_info:
        run_external_command(
            ["denoise", "-i", "input.tif"],
            cwd="/work/row",
            context="CImgDenoising",
        )

    message = str(exc_info.value)
    assert "CImgDenoising" in message
    assert "denoise -i input.tif" in message
    assert "SIGTRAP" in message
    assert "Working directory: /work/row" in message
    assert "native crash details" in message
    assert exc_info.value.signal_name == "SIGTRAP"
    assert exc_info.value.returncode == -5
    assert isinstance(exc_info.value.__cause__, subprocess.CalledProcessError)


def test_run_external_command_reports_exit_status(monkeypatch) -> None:
    def fake_run(command, run_kwargs):
        raise subprocess.CalledProcessError(
            returncode=2,
            cmd=command,
            output="partial output",
            stderr="usage error",
        )

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    with pytest.raises(ExternalCommandError) as exc_info:
        run_external_command(["atlas", "-bad"], context="Atlas")

    message = str(exc_info.value)
    assert "Atlas" in message
    assert "exited with status 2" in message
    assert "partial output" in message
    assert "usage error" in message
    assert exc_info.value.signal_name is None


def test_run_external_command_reports_launch_failures(monkeypatch) -> None:
    def fake_run(command, run_kwargs):
        raise FileNotFoundError("No such file or directory")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    with pytest.raises(ExternalCommandError) as exc_info:
        run_external_command(["missing-cli", "--version"], context="VersionReport")

    message = str(exc_info.value)
    assert "Unable to start external command" in message
    assert "VersionReport" in message
    assert "missing-cli --version" in message
    assert "No such file or directory" in message
    assert exc_info.value.returncode is None


def test_run_external_command_with_staged_output_copies_to_final_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, run_kwargs):
        calls.append(command)
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("staged result")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    final_output = tmp_path / "long" / "requested-output.tif"
    run_external_command_with_staged_output(
        ["external-tool", "-o", final_output],
        output_path=final_output,
        context="ExampleTool",
    )

    assert final_output.read_text() == "staged result"
    staged_output = Path(calls[0][calls[0].index("-o") + 1])
    assert staged_output.name == final_output.name
    assert staged_output != final_output
    assert staged_output.parent != final_output.parent


def test_run_external_command_with_staged_output_reports_missing_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_run(command, run_kwargs):
        return None

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    final_output = tmp_path / "missing.tif"
    with pytest.raises(FileNotFoundError) as exc_info:
        run_external_command_with_staged_output(
            ["external-tool", "-o", final_output],
            output_path=final_output,
            context="ExampleTool",
        )

    message = str(exc_info.value)
    assert "did not create staged output" in message
    assert str(final_output) in message
