from __future__ import annotations

import importlib.util
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

pytestmark = pytest.mark.package_tools


def test_common_exports_exclude_moved_heavy_tools() -> None:
    import bioimageflow_common_tools as common

    for name in [
        "ConvertImage",
        "CellposeSAM",
        "Cellpose3",
        "StarDistSegmenter",
        "Atlas",
    ]:
        assert not hasattr(common, name)

    assert hasattr(common, "Files")
    assert hasattr(common, "TableFromCsv")
    assert hasattr(common, "WriteTable")
    assert hasattr(common, "FilterTableRows")
    assert hasattr(common, "SelectColumns")
    assert hasattr(common, "ExtractChannel")
    assert hasattr(common, "LabelOverlaps")
    assert importlib.util.find_spec("bioimageflow_common_tools.cellpose_v3") is None
    assert importlib.util.find_spec("bioimageflow_common_tools.cellpose_sam") is None
    assert (
        importlib.util.find_spec("bioimageflow_common_tools.stardist_segmenter") is None
    )


def test_common_docs_separate_public_tools_from_legacy_module_docs() -> None:
    index = (
        Path(__file__).parents[1]
        / "docs"
        / "index.md"
    ).read_text()

    assert "## Public Tools" in index
    assert "## Legacy Module Documentation" in index
    public_tools = index.split("## Legacy Module Documentation", maxsplit=1)[0]
    assert "[Files]" in public_tools
    assert "[Mosaic]" in public_tools
    assert "[Atlas]" not in public_tools
    assert "[ConvertImage]" not in public_tools


def test_common_pyproject_declares_public_tool_runtime_dependencies() -> None:
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    dependencies = {
        dependency.split(">=", maxsplit=1)[0].split("==", maxsplit=1)[0].lower()
        for dependency in pyproject["project"]["dependencies"]
    }

    assert {"pandas", "imageio", "numpy", "pillow"} <= dependencies


def test_generate_creates_parameter_table_and_resolves_output_schema() -> None:
    from bioimageflow_common_tools import Generate

    inputs = serialize_input_schema(Generate)
    assert inputs["column_name"]["type"] == "str"
    assert inputs["values"]["type"] == "list"
    assert serialize_output_schema(Generate) == {}

    schema = Generate.resolve_outputs({"column_name": "threshold"})
    assert schema == {"threshold": {"type": "any", "default": None, "image_spec": None}}

    table = Generate().transform(
        None,
        Arguments(column_name="threshold", values=[0.1, 0.2, 0.5]),
    )
    assert table.to_dict("list") == {"threshold": [0.1, 0.2, 0.5]}


@pytest.mark.skipif(
    importlib.util.find_spec("SimpleITK") is None,
    reason="ConnectedComponents complete test requires optional SimpleITK",
)
@pytest.mark.complete
def test_connected_components_labels_binary_objects(tmp_path: Path) -> None:
    from bioimageflow_common_tools import ConnectedComponents

    image = np.zeros((12, 12), dtype=np.uint8)
    image[1:4, 1:4] = 1
    image[7:10, 8:11] = 1
    input_path = tmp_path / "binary.tif"
    output_path = tmp_path / "labels.tif"
    iio.imwrite(input_path, image)

    result = ConnectedComponents().process_row(
        Arguments(input_image=input_path, output_image=output_path)
    )

    labels = iio.imread(output_path)
    assert result.num_labels == 2
    assert labels.max() == 2
    assert labels[2, 2] != 0
    assert labels[8, 9] != 0
    assert labels[2, 2] != labels[8, 9]
