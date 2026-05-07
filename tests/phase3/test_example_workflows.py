from pathlib import Path

import importlib.util
import pandas as pd


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.parent.name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase3_example_workflows_execute(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    examples = [
        (
            root / "example-workflows" / "phase3_puncta" / "workflow.py",
            "summary_csv",
            {"summary_csv", "label_count"},
        ),
        (
            root / "example-workflows" / "phase3_restoration_benchmark" / "workflow.py",
            "metrics_csv",
            {"clean_image", "degraded_image", "restored_image", "metrics_csv"},
        ),
        (
            root / "example-workflows" / "phase3_tracking" / "workflow.py",
            "metrics_csv",
            {"metrics_csv", "track_count", "mean_track_length"},
        ),
    ]

    for workflow_path, file_column, expected_columns in examples:
        module = _load_module(workflow_path)
        wf, node = module.build_workflow(
            storage_path=str(tmp_path / workflow_path.parent.name)
        )
        result = wf.compute(node)
        assert expected_columns <= set(result.columns)
        assert len(result) == 1
        output_path = Path(result.iloc[0][file_column])
        assert output_path.exists()
        if output_path.suffix == ".csv":
            assert not pd.read_csv(output_path).empty
