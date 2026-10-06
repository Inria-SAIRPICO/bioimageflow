from __future__ import annotations

import subprocess
import os
import sys

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
    monkeypatch.setattr(external.shutil, "which", lambda _name, **_kwargs: None)
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
        return subprocess.CompletedProcess(command, 0)

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
        return subprocess.CompletedProcess(command, 0)

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


@pytest.mark.parametrize("exit_status", [0, 3])
def test_real_child_publishes_only_success(tmp_path, exit_status):
    output = tmp_path / "result.txt"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('9'); print('diagnostic'); sys.exit(int(sys.argv[2]))",
        output,
        exit_status,
    ]
    result = run_external_command_with_staged_output(
        command,
        output_path=output,
        staging_parent=tmp_path / "stage",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == exit_status
    assert result.stdout.strip() == "diagnostic"
    assert output.exists() == (exit_status == 0)
    if exit_status == 0:
        assert output.read_text() == "9"
    assert list((tmp_path / "stage").iterdir()) == []


@pytest.mark.parametrize("broken_link", [False, True])
def test_occupied_final_refuses_before_child(monkeypatch, tmp_path, broken_link):
    output = tmp_path / "result.txt"
    if broken_link:
        output.symlink_to(tmp_path / "absent")
    else:
        output.write_text("independent")
    inode = output.lstat().st_ino
    calls = []
    monkeypatch.setattr(external, "_run_subprocess", lambda *args: calls.append(args))
    with pytest.raises(FileExistsError):
        run_external_command_with_staged_output(["tool", output], output_path=output)
    assert calls == []
    assert output.lstat().st_ino == inode


def test_real_child_symlink_output_is_not_published(tmp_path):
    sentinel = tmp_path / "foreign.txt"
    sentinel.write_text("independent")
    inode = sentinel.stat().st_ino
    output = tmp_path / "result.txt"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).symlink_to(sys.argv[2])",
        output,
        sentinel,
    ]
    with pytest.raises(ValueError, match="regular file"):
        run_external_command_with_staged_output(
            command, output_path=output, staging_parent=tmp_path / "stage"
        )
    assert not output.exists()
    assert sentinel.read_text() == "independent"
    assert sentinel.stat().st_ino == inode
    assert list((tmp_path / "stage").iterdir()) == []


def test_late_final_owner_and_predictable_temporary_are_preserved(
    monkeypatch, tmp_path
):
    output = tmp_path / "result.txt"
    predicted = tmp_path / f".{output.name}.tmp-{os.getpid()}"
    predicted.write_text("unrelated temporary")
    predicted_inode = predicted.stat().st_ino
    winner_inode = []

    def fake_run(command, _kwargs):
        Path(command[-1]).write_text("candidate")
        output.write_text("late winner")
        winner_inode.append(output.stat().st_ino)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(external, "_run_subprocess", fake_run)
    with pytest.raises(FileExistsError):
        run_external_command_with_staged_output(
            ["tool", output], output_path=output, staging_parent=tmp_path / "stage"
        )
    assert output.read_text() == "late winner"
    assert output.stat().st_ino == winner_inode[0]
    assert predicted.read_text() == "unrelated temporary"
    assert predicted.stat().st_ino == predicted_inode
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["result.txt", predicted.name, "stage"]
    )
    assert list((tmp_path / "stage").iterdir()) == []


def test_supplied_relative_path_selects_cli_before_private_neighbor(
    monkeypatch, tmp_path
):
    selected = tmp_path / "selected" / "tool"
    selected.parent.mkdir()
    selected.write_text("selected")
    neighbor = tmp_path / "private" / "tool"
    neighbor.parent.mkdir()
    neighbor.write_text("neighbor")
    calls = []
    searches = []

    def which(name, *, path):
        searches.append((name, path))
        return str(selected)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(external.sys, "executable", str(neighbor.parent / "python"))
    monkeypatch.setattr(external.shutil, "which", which)
    monkeypatch.setattr(external.subprocess, "run", run)
    environment = {"PATH": "selected"}
    run_external_command(["tool", "--version"], cwd=tmp_path, env=environment)
    assert searches == [("tool", str(selected.parent))]
    assert calls[0][0] == [str(selected), "--version"]
    assert calls[0][1]["env"] == environment == {"PATH": "selected"}


@pytest.mark.parametrize("readonly_primary", [False, True])
def test_cleanup_failure_preserves_primary_and_retry_owner(
    monkeypatch, tmp_path, caplog, readonly_primary
):
    output = tmp_path / "result.txt"

    class ReadonlyError(RuntimeError):
        def __setattr__(self, _name, _value):
            raise AttributeError("read-only exception")

    primary = (
        ReadonlyError("child failure")
        if readonly_primary
        else RuntimeError("child failure")
    )
    original_remove = external.shutil.rmtree

    def fail_child(*_args):
        raise primary

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("contingent cleanup failure")

    monkeypatch.setattr(external, "_run_subprocess", fail_child)
    monkeypatch.setattr(external.shutil, "rmtree", fail_cleanup)
    with pytest.raises(RuntimeError) as error:
        run_external_command_with_staged_output(
            ["tool", output], output_path=output, staging_parent=tmp_path / "stage"
        )
    assert error.value is primary
    if readonly_primary:
        pending = list((tmp_path / "stage").iterdir())
        assert len(pending) == 1 and pending[0].is_dir()
        assert str(pending[0]) in caplog.text
        assert not hasattr(primary, "_external_command_cleanup")
        original_remove(pending[0])
        return
    assert primary.external_cleanup["published_output"] is None
    pending = primary.external_cleanup["pending_paths"]
    assert len(pending) == 1 and Path(pending[0]).is_dir()
    monkeypatch.setattr(external.shutil, "rmtree", original_remove)
    primary._external_command_cleanup.close()
    assert not Path(pending[0]).exists()
    assert primary._external_command_cleanup.pending_paths == ()
