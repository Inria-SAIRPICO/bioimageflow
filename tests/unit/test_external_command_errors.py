from __future__ import annotations

import subprocess

import pytest

from bioimageflow_core import ExternalCommandError, run_external_command


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
