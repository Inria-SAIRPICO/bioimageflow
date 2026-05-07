"""Atlas wrapper keeps implicit CLI outputs out of the process cwd."""

from pathlib import Path

import bioimageflow_common_tools.atlas as atlas_module
from bioimageflow_common_tools.atlas import Atlas
from bioimageflow_core import Arguments, ExecutionContext


def test_atlas_reference_file_is_packaged():
    blobs_file = Path(atlas_module.__file__).parent / "data" / "blobs.txt"

    assert blobs_file.is_file()
    assert blobs_file.stat().st_size > 0


def test_atlas_runs_external_commands_in_execution_work_dir(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr("bioimageflow_common_tools.atlas.subprocess.run", fake_run)

    output_path = tmp_path / "assets" / "detections.tif"
    context = ExecutionContext(
        run_dir=tmp_path,
        assets_dir=tmp_path / "assets",
        work_dir=tmp_path / "work" / "rows" / "000000",
        rows_dir=tmp_path / "work" / "rows",
        row_index="0",
    )

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
    assert calls[-1][1]["cwd"] == context.work_dir
    assert not (Path.cwd() / "LoG.tif").exists()


def test_atlas_blobsref_fallback_uses_absolute_scratch_path(tmp_path, monkeypatch):
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
    context = ExecutionContext(
        run_dir=relative_root,
        assets_dir=relative_root / "assets",
        work_dir=relative_root / "work" / "rows" / "000000",
        rows_dir=relative_root / "work" / "rows",
        row_index="0",
    )

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
    assert Path(blobsref_call[0][2]).is_absolute()
    assert Path(blobsref_call[0][2]).name == "blobs.txt"
    assert blobsref_call[1]["cwd"] == context.work_dir

    atlas_call = calls[1]
    assert atlas_call[0][0] == "atlas"
    assert atlas_call[0][2] == blobsref_call[0][2]
