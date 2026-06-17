from pathlib import Path

import importlib.util
import pytest


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
            "puncta_analysis",
            None,
            {"label", "spot_count", "mean_intensity", "total_intensity", "label_count"},
            1,
            id="puncta_analysis",
        ),
        pytest.param(
            "restoration_benchmark",
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
            id="restoration_benchmark",
        ),
        pytest.param(
            "tracking_analysis",
            None,
            {"track_id", "track_length", "track_count", "mean_track_length"},
            1,
            id="tracking_analysis",
        ),
    ],
)
def test_specialized_example_workflow_executes(
    tmp_path: Path,
    workflow_name: str,
    artifact_column: str | None,
    expected_columns: set[str],
    min_rows: int,
) -> None:
    root = Path(__file__).resolve().parents[2]
    workflow_path = root / "example-workflows" / workflow_name / "workflow.py"
    module = _load_module(workflow_path)
    wf, node = module.build_workflow(storage_path=str(tmp_path / workflow_name))

    result = wf.compute(node)

    assert expected_columns <= set(result.columns)
    assert len(result) >= min_rows
    if artifact_column is not None:
        assert Path(result.iloc[0][artifact_column]).exists()
