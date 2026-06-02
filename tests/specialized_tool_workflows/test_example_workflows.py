from pathlib import Path

import importlib.util
import pandas as pd
import pytest


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.parent.name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("workflow_name", "file_column", "expected_columns"),
    [
        pytest.param(
            "puncta_analysis",
            "summary_csv",
            {"summary_csv", "label_count"},
            id="puncta_analysis",
        ),
        pytest.param(
            "restoration_benchmark",
            "metrics_csv",
            {"clean_image", "degraded_image", "restored_image", "metrics_csv"},
            id="restoration_benchmark",
        ),
        pytest.param(
            "tracking_analysis",
            "metrics_csv",
            {"metrics_csv", "track_count", "mean_track_length"},
            id="tracking_analysis",
        ),
    ],
)
def test_specialized_example_workflow_executes(
    tmp_path: Path,
    workflow_name: str,
    file_column: str,
    expected_columns: set[str],
) -> None:
    root = Path(__file__).resolve().parents[2]
    workflow_path = root / "example-workflows" / workflow_name / "workflow.py"
    module = _load_module(workflow_path)
    wf, node = module.build_workflow(storage_path=str(tmp_path / workflow_name))
    wf.use_wetlands = False

    result = wf.compute(node)

    assert expected_columns <= set(result.columns)
    assert len(result) == 1
    output_path = Path(result.iloc[0][file_column])
    assert output_path.exists()
    if output_path.suffix == ".csv":
        assert not pd.read_csv(output_path).empty
