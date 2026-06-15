"""Atlas wrapper keeps implicit CLI outputs out of the process cwd."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bioimageflow_common_tools.atlas as atlas_module
from bioimageflow_common_tools.atlas import Atlas
from bioimageflow_core import Arguments, ExecutionContext

import pytest

pytestmark = pytest.mark.package_tools


def test_atlas_reference_file_is_packaged():
    blobs_file = Path(atlas_module.__file__).parent / "data" / "blobs.txt"

    assert blobs_file.is_file()
    assert blobs_file.stat().st_size > 0


def _execution_context(run_dir: Path, row_name: str = "000000") -> ExecutionContext:
    return ExecutionContext(
        run_dir=run_dir,
        assets_dir=run_dir / "assets",
        work_dir=run_dir / "work",
        rows_dir=run_dir / "work" / "rows",
        row_dir=run_dir / "work" / "rows" / row_name,
        batch_dir=None,
        row_index=row_name,
    )


def test_atlas_runs_external_command_in_execution_row_dir(tmp_path, monkeypatch):
    calls = []

    def fake_run_staged(command, **kwargs):
        calls.append((command, kwargs))
        Path(kwargs["output_path"]).write_text("detections")

    monkeypatch.setattr(
        "bioimageflow_common_tools.atlas.run_external_command_with_staged_output",
        fake_run_staged,
    )

    output_path = tmp_path / "assets" / "detections.tif"
    context = _execution_context(tmp_path)

    result = Atlas().process_row(
        Arguments(
            input_image=tmp_path / "input.tif",
            output_image=output_path,
            gaussian_std=None,
            p_value=None,
            area_lim=None,
            verbose=False,
        ),
        context=context,
    )

    assert Path(result.output_image) == output_path
    assert output_path.read_text() == "detections"
    assert calls
    assert calls[-1][0][0] == "atlas"
    assert calls[-1][1]["cwd"] == context.row_dir
    assert calls[-1][1]["output_path"] == output_path
    assert not (Path.cwd() / "LoG.tif").exists()


def test_atlas_blobsref_fallback_uses_shared_work_atlas_path(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "blobsref":
            Path(command[2]).write_text("reference")

    def fake_run_staged(command, **kwargs):
        calls.append((command, kwargs))
        Path(kwargs["output_path"]).write_text("detections")

    fake_package_file = tmp_path / "missing_package_data" / "atlas.py"
    fake_package_file.parent.mkdir()
    fake_package_file.write_text("")
    monkeypatch.setattr(atlas_module, "__file__", str(fake_package_file))
    monkeypatch.setattr("bioimageflow_common_tools.atlas.run_external_command", fake_run)
    monkeypatch.setattr(
        "bioimageflow_common_tools.atlas.run_external_command_with_staged_output",
        fake_run_staged,
    )
    monkeypatch.chdir(tmp_path)

    relative_root = Path("relative_run")
    context = _execution_context(relative_root)

    Atlas().process_row(
        Arguments(
            input_image=tmp_path / "input.tif",
            output_image=tmp_path / "assets" / "detections.tif",
            gaussian_std=None,
            p_value=None,
            area_lim=None,
            verbose=False,
        ),
        context=context,
    )

    blobsref_call = calls[0]
    assert blobsref_call[0][0] == "blobsref"
    expected_blobs_path = (context.work_dir / "atlas" / "blobs.txt").resolve()
    blobs_tmp_path = Path(blobsref_call[0][2])
    assert blobs_tmp_path == expected_blobs_path.with_suffix(".txt.tmp")
    assert Path(blobsref_call[1]["cwd"]) == expected_blobs_path.parent

    atlas_call = calls[1]
    assert atlas_call[0][0] == "atlas"
    assert atlas_call[0][2] == str(expected_blobs_path)
    assert atlas_call[1]["cwd"] == context.row_dir
    assert atlas_call[1]["output_path"] == tmp_path / "assets" / "detections.tif"


def test_atlas_blobsref_fallback_is_generated_once_for_parallel_rows(
    tmp_path, monkeypatch
):
    calls = []
    calls_lock = threading.Lock()

    def fake_run(command, **kwargs):
        with calls_lock:
            calls.append((command, kwargs))
        if command[0] == "blobsref":
            time.sleep(0.05)
            Path(command[2]).write_text("reference")

    def fake_run_staged(command, **kwargs):
        with calls_lock:
            calls.append((command, kwargs))
        Path(kwargs["output_path"]).write_text("detections")

    fake_package_file = tmp_path / "missing_package_data" / "atlas.py"
    fake_package_file.parent.mkdir()
    fake_package_file.write_text("")
    monkeypatch.setattr(atlas_module, "__file__", str(fake_package_file))
    monkeypatch.setattr("bioimageflow_common_tools.atlas.run_external_command", fake_run)
    monkeypatch.setattr(
        "bioimageflow_common_tools.atlas.run_external_command_with_staged_output",
        fake_run_staged,
    )

    def run_row(i: int) -> None:
        Atlas().process_row(
            Arguments(
                input_image=tmp_path / f"input_{i}.tif",
                output_image=tmp_path / "assets" / f"detections_{i}.tif",
                gaussian_std=None,
                p_value=None,
                area_lim=None,
                verbose=False,
            ),
            context=_execution_context(tmp_path, f"{i:06d}"),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(run_row, range(4)))

    blobsref_calls = [call for call in calls if call[0][0] == "blobsref"]
    atlas_calls = [call for call in calls if call[0][0] == "atlas"]

    expected_blobs_path = tmp_path / "work" / "atlas" / "blobs.txt"
    assert len(blobsref_calls) == 1
    assert expected_blobs_path.read_text() == "reference"
    assert not expected_blobs_path.with_suffix(".txt.tmp").exists()
    assert not (expected_blobs_path.parent / ".blobsref.lock").exists()
    assert {call[0][2] for call in atlas_calls} == {str(expected_blobs_path)}
    assert {call[1]["cwd"] for call in atlas_calls} == {
        tmp_path / "work" / "rows" / f"{i:06d}" for i in range(4)
    }
