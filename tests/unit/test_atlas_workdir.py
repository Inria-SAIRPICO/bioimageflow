"""Atlas wrapper keeps implicit CLI outputs out of the process cwd."""

from pathlib import Path

import bioimageflow_common_tools.atlas as atlas_module
from bioimageflow_common_tools.atlas import Atlas
from bioimageflow_core import Arguments, ExecutionContext


def test_atlas_reference_file_is_packaged():
    blobs_file = Path(atlas_module.__file__).parent / "data" / "blobs.txt"

    assert blobs_file.is_file()
    assert blobs_file.stat().st_size > 0


def _execution_context(run_dir: Path) -> ExecutionContext:
    return ExecutionContext(
        run_dir=run_dir,
        assets_dir=run_dir / "assets",
        work_dir=run_dir / "work",
        rows_dir=run_dir / "work" / "rows",
        row_dir=run_dir / "work" / "rows" / "000000",
        batch_dir=None,
        row_index="0",
    )


def test_atlas_runs_external_command_in_execution_row_dir(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr("bioimageflow_common_tools.atlas.subprocess.run", fake_run)

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
    assert calls
    assert calls[-1][0][0] == "atlas"
    assert calls[-1][1]["cwd"] == context.row_dir
    assert not (Path.cwd() / "LoG.tif").exists()


def test_atlas_blobsref_fallback_uses_shared_work_atlas_path(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    fake_package_file = tmp_path / "missing_package_data" / "atlas.py"
    fake_package_file.parent.mkdir()
    fake_package_file.write_text("")
    monkeypatch.setattr(atlas_module, "__file__", str(fake_package_file))
    monkeypatch.setattr("bioimageflow_common_tools.atlas.subprocess.run", fake_run)
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
    blobs_path = Path(blobsref_call[0][2])
    expected_blobs_path = (context.work_dir / "atlas" / "blobs.txt").resolve()
    assert blobs_path == expected_blobs_path
    assert Path(blobsref_call[1]["cwd"]) == expected_blobs_path.parent

    atlas_call = calls[1]
    assert atlas_call[0][0] == "atlas"
    assert atlas_call[0][2] == blobsref_call[0][2]
    assert atlas_call[1]["cwd"] == context.row_dir
