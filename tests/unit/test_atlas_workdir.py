"""Atlas wrapper keeps implicit CLI outputs out of the process cwd."""

from pathlib import Path

from bioimageflow_common_tools.atlas import Atlas
from bioimageflow_core import Arguments, ExecutionContext


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
