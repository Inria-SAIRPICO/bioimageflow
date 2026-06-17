"""Documentation contracts for package dependency and backend wording."""

from pathlib import Path


ROOT = Path(__file__).parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_restoration_numpy_baseline_docs_do_not_advertise_scipy_or_skimage_backends() -> None:
    docs = [
        "packages/bioimageflow-restoration-tools/docs/tools/gaussian_denoise.md",
        "packages/bioimageflow-restoration-tools/docs/tools/background_subtract.md",
        "packages/bioimageflow-restoration-tools/docs/tools/unsharp_mask.md",
        "packages/bioimageflow-restoration-tools/docs/tools/richardson_lucy_restoration.md",
    ]
    offenders = [
        path
        for path in docs
        if "scikit-image" in _text(path).lower() or "scipy" in _text(path).lower()
    ]

    assert offenders == []


def test_restoration_package_docs_describe_skimage_as_optional_for_specific_tools() -> None:
    text = _text("packages/bioimageflow-restoration-tools/docs/index.md")

    assert "install-time libraries are imageio and numpy" in text.lower()
    assert "restoreimage" in text
    assert "benchmarkrestoration" in text
    assert "optional scikit-image" in text.lower()


def test_segmentation_index_separates_install_time_libraries_from_model_environments() -> None:
    text = _text("packages/bioimageflow-segmentation-tools/docs/index.md")
    core_line = next(line for line in text.splitlines() if line.startswith("Install-time libraries"))

    assert "Cellpose" not in core_line
    assert "StarDist" not in core_line
    assert "TensorFlow" not in core_line
    assert "isolated `EnvironmentSpec`" in text
    assert "complete`, `wetlands`, and `model_runtime`" in text


def test_common_tools_docs_describe_simpleitk_as_isolated_runtime_dependency() -> None:
    for path in [
        "packages/bioimageflow-common-tools/docs/index.md",
        "packages/bioimageflow-common-tools/README.md",
    ]:
        text = _text(path)
        assert "install-time libraries" in text.lower()
        assert "simpleitk" in text.lower()
        assert "EnvironmentSpec" in text


def test_spot_and_tracking_docs_include_pandas_for_dataframe_tools() -> None:
    for path in [
        "packages/bioimageflow-spot-tools/docs/index.md",
        "packages/bioimageflow-tracking-tools/docs/index.md",
    ]:
        text = _text(path)
        assert "pandas" in text.lower()
