from pathlib import Path

import importlib.util
import sys
import types
from typing import Any
import pytest

from tests.workflow_catalog.test_workflows import (
    _write_cell_counting_input,
    _write_restoration_pair,
)


pytestmark = pytest.mark.acceptance


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.parent.name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("workflow_name", "artifact_column", "expected_columns", "min_rows"),
    [
        pytest.param(
            "cell_counting_phenotyping",
            None,
            {
                "image",
                "object_count",
                "mean_area",
                "total_area",
                "mean_intensity",
                "mean_perimeter",
            },
            1,
            id="cell_counting_phenotyping",
        ),
        pytest.param(
            "low_snr_restoration",
            "restored_image",
            {
                "clean_image",
                "degraded_image",
                "restored_image",
                "mse_degraded",
                "mse_restored",
                "degraded_psnr",
                "restored_psnr",
            },
            1,
            id="low_snr_restoration",
        ),
        pytest.param(
            "live_cell_tracking",
            None,
            {"tracker", "track_id", "track_length", "track_count", "mean_track_length"},
            2,
            id="live_cell_tracking",
        ),
    ],
)
def test_specialized_example_workflow_executes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_name: str,
    artifact_column: str | None,
    expected_columns: set[str],
    min_rows: int,
) -> None:
    if workflow_name == "low_snr_restoration":
        careamics_module = types.ModuleType("careamics")

        def fake_predict(image: Any, *, checkpoint: Path | None) -> Any:
            return image

        careamics_module.predict = fake_predict
        monkeypatch.setitem(sys.modules, "careamics", careamics_module)
    if workflow_name == "live_cell_tracking":
        from bioimageflow_tracking_tools import LinkObjects

        def fake_link_objects(df: Any, *, max_distance: float) -> Any:
            return LinkObjects().transform(df, type("Args", (), {"max_distance": max_distance})())

        ultrack_module = types.ModuleType("ultrack")
        btrack_module = types.ModuleType("btrack")
        ultrack_module.link_objects = fake_link_objects
        btrack_module.link_objects = fake_link_objects
        monkeypatch.setitem(sys.modules, "ultrack", ultrack_module)
        monkeypatch.setitem(sys.modules, "btrack", btrack_module)

    root = Path(__file__).resolve().parents[2]
    workflow_path = root / "example-workflows" / workflow_name / "workflow.py"
    module = _load_module(workflow_path)
    kwargs: dict[str, Any] = {
        "storage_path": str(tmp_path / workflow_name),
        "engine": "direct",
    }
    if workflow_name == "cell_counting_phenotyping":
        kwargs["input_image"] = str(_write_cell_counting_input(tmp_path / "bbbc038_crop.tif"))
    if workflow_name == "low_snr_restoration":
        clean_image, degraded_image = _write_restoration_pair(tmp_path / "restoration_data")
        checkpoint = tmp_path / "careamics_checkpoint.ckpt"
        checkpoint.write_text("fake checkpoint")
        kwargs["clean_image"] = str(clean_image)
        kwargs["degraded_image"] = str(degraded_image)
        kwargs["checkpoint"] = str(checkpoint)
    wf, node = module.build_workflow(**kwargs)

    result = wf.compute(node)

    assert expected_columns <= set(result.columns)
    assert len(result) >= min_rows
    if artifact_column is not None:
        assert Path(result.iloc[0][artifact_column]).exists()
