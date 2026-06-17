"""Documentation contracts for package dependency and backend wording."""

from pathlib import Path
import re


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
    lowered = text.lower()

    assert "install-time libraries are imageio and numpy" in lowered
    assert "restoreimage" in lowered
    assert "benchmarkrestoration" in lowered
    assert "optional scikit-image" in lowered


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


def test_segmentation_docs_do_not_list_tifffile_as_install_time_dependency() -> None:
    text = _text("packages/bioimageflow-segmentation-tools/docs/index.md")
    install_line = next(line for line in text.splitlines() if line.startswith("Install-time libraries"))

    assert "tifffile" not in install_line.lower()
    assert "EnvironmentSpec" in text


def test_package_index_links_are_standalone_relative_links() -> None:
    offenders = []
    link_pattern = re.compile(r"\[[^\]]+\]\((#[^)]+)\)")

    for path in sorted((ROOT / "packages").glob("*/docs/index.md")):
        for anchor in link_pattern.findall(path.read_text()):
            offenders.append(f"{path.relative_to(ROOT)}: {anchor}")

    assert offenders == []


def test_tool_package_reference_documents_release_and_ci_contract() -> None:
    text = _text("docs/source/reference/tool_packages.md")

    for required in [
        "Release and CI Contract",
        "lockstep versions",
        "Python `>=3.10`",
        "uv run ruff check .",
        "uv run pyright",
        'uv run pytest -m "not slow"',
        "uv build --all-packages --out-dir dist/packages",
        "complete-test jobs are manual or scheduled",
    ]:
        assert required in text
