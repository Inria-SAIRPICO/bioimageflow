"""Lightweight image IO tools."""

import json
from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)


LIGHTWEIGHT_IO_ENV = EnvironmentSpec(
    name="bioimageflow-lightweight-io",
    dependencies={
        "python": "3.12",
        "pip": ["imageio", "numpy", "tifffile"],
    },
)

BIOIO_ENV = EnvironmentSpec(
    name="bioimageflow-bioio",
    dependencies={
        "python": "3.12",
        "pip": [
            "bioio",
            "bioio-ome-tiff",
            "bioio-ome-zarr",
            "bioio-imageio",
            "bioio-tifffile",
        ],
    },
)


class ReadImage(ProcessingTool):
    """Read an image file and write it to a normalized downstream image path."""

    display_name = "Read Image"
    documentation = "Read an image file with imageio and write a workflow-local copy."
    category = Category.CONVERSION
    tags = ["io", "image"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image file to read.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Output image",
                description="Workflow-local image copy.",
            ),
        ] = Template("{input_image.stem}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = iio.imread(arguments.input_image)
        iio.imwrite(output_path, data)
        return self.Outputs(output_image=output_path)


class SelectDimensions(ProcessingTool):
    """Select channel, z, and/or time indices from a raster image."""

    display_name = "Select Dimensions"
    documentation = "Select channel, z, and time indices from CZYX, TCYX, or TZYX images."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                layouts={
                    Layout.VOLUMETRIC_CHANNEL,
                    Layout.PLANAR_TIME_CHANNEL,
                    Layout.VOLUMETRIC_TIME,
                }
            ),
            GUIMeta(
                display_name="Input image",
                description="Image to slice. Axes are inferred from dimensionality and selected indices.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        layout: Annotated[str, GUIMeta(
            display_name="Layout",
            description="Axis order for the input image, such as CZYX, TCYX, or TZYX.",
            connectable=Connectable.NEVER,
        )] = "CZYX"
        channel: Annotated[int | None, GUIMeta(
            display_name="Channel",
            description="Optional index for the C axis.",
            min=0,
            step=1,
        )] = None
        z: Annotated[int | None, GUIMeta(
            display_name="Z",
            description="Optional index for the Z axis.",
            min=0,
            step=1,
        )] = None
        timepoint: Annotated[int | None, GUIMeta(
            display_name="Timepoint",
            description="Optional index for the T axis.",
            min=0,
            step=1,
        )] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(
                display_name="Selected image",
                description="Image after requested dimension selection.",
            ),
        ] = Template("{input_image.stem}_selected{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        data = iio.imread(arguments.input_image)
        selected = _select_dimensions(
            data,
            layout=arguments.layout,
            channel=arguments.channel,
            z=arguments.z,
            timepoint=arguments.timepoint,
        )
        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output_path, selected)
        return self.Outputs(output_image=output_path)


class WriteOmeTiff(ProcessingTool):
    """Write an image as OME-TIFF using tifffile."""

    display_name = "Write OME-TIFF"
    documentation = "Write an image to OME-TIFF with tifffile."
    category = Category.CONVERSION
    tags = ["io", "ome-tiff"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image to write as OME-TIFF.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        dimension_order: Annotated[str | None, GUIMeta(
            display_name="Dimension order",
            description="Optional OME axis order. Defaults are inferred from image dimensionality.",
            connectable=Connectable.NEVER,
        )] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(formats={"ome-tiff"}),
            GUIMeta(
                display_name="OME-TIFF image",
                description="Written OME-TIFF image.",
            ),
        ] = Template("{input_image.stem}.ome.tiff")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import tifffile

        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = iio.imread(arguments.input_image)
        axes = _dimension_order_for_shape(data.shape, arguments.dimension_order)
        tifffile.imwrite(output_path, data, ome=True, metadata={"axes": axes})
        return self.Outputs(output_image=output_path)


class WriteOmeZarr(ProcessingTool):
    """Write an image as a minimal OME-NGFF/Zarr v2 directory."""

    display_name = "Write OME-Zarr"
    documentation = "Write a single-scale uncompressed OME-Zarr v2 directory."
    category = Category.CONVERSION
    tags = ["io", "ome-zarr", "zarr"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image to write as OME-Zarr.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        output_path: Annotated[
            Path,
            ImageSpec(formats={"ome-zarr"}),
            GUIMeta(
                display_name="OME-Zarr path",
                description="Written OME-Zarr directory.",
            ),
        ] = Template("{input_image.stem}.ome.zarr")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        output_path = Path(arguments.output_path)
        data = iio.imread(arguments.input_image)
        _write_minimal_ome_zarr(data, output_path)
        return self.Outputs(output_path=output_path)


def _select_dimensions(
    data: Any,
    *,
    layout: str,
    channel: int | None,
    z: int | None,
    timepoint: int | None,
) -> Any:
    normalized_layout = layout.upper()
    if len(normalized_layout) != data.ndim:
        raise ValueError(
            f"Layout {layout!r} has {len(normalized_layout)} axes, "
            f"but image has {data.ndim} dimensions."
        )

    selectors: list[int | slice] = [slice(None)] * data.ndim
    for axis, index in {"T": timepoint, "C": channel, "Z": z}.items():
        if index is None:
            continue
        if axis not in normalized_layout:
            raise ValueError(f"Cannot select {axis}: layout {layout!r} has no {axis} axis.")
        selectors[normalized_layout.index(axis)] = index
    return data[tuple(selectors)]


def _dimension_order_for_shape(shape: tuple[int, ...], dimension_order: str | None) -> str:
    if dimension_order is not None:
        normalized = dimension_order.upper()
        if len(normalized) != len(shape):
            raise ValueError(
                f"Dimension order {dimension_order!r} has {len(normalized)} axes, "
                f"but image has {len(shape)} dimensions."
            )
        return normalized

    default_by_ndim = {
        2: "YX",
        3: "ZYX",
        4: "CZYX",
        5: "TCZYX",
    }
    if len(shape) not in default_by_ndim:
        raise ValueError(
            f"Cannot infer OME dimension order for {len(shape)}D image; "
            "set dimension_order explicitly."
        )
    return default_by_ndim[len(shape)]


def _write_minimal_ome_zarr(data: Any, output_path: Path) -> None:
    import numpy as np

    array = np.asarray(data)
    output_path.mkdir(parents=True, exist_ok=True)
    level_path = output_path / "0"
    level_path.mkdir(exist_ok=True)

    axes = _axes_for_shape(array.shape)
    (output_path / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
    (output_path / ".zattrs").write_text(
        json.dumps(
            {
                "multiscales": [
                    {
                        "version": "0.4",
                        "axes": [{"name": axis, "type": _axis_type(axis)} for axis in axes],
                        "datasets": [{"path": "0"}],
                    }
                ]
            }
        )
    )
    (level_path / ".zarray").write_text(
        json.dumps(
            {
                "zarr_format": 2,
                "shape": list(array.shape),
                "chunks": list(array.shape),
                "dtype": array.dtype.str,
                "compressor": None,
                "fill_value": 0,
                "order": "C",
                "filters": None,
            }
        )
    )
    chunk_key = ".".join("0" for _ in array.shape) if array.ndim else "0"
    (level_path / chunk_key).write_bytes(np.ascontiguousarray(array).tobytes(order="C"))


def _axes_for_shape(shape: tuple[int, ...]) -> list[str]:
    names_by_ndim = {
        2: ["y", "x"],
        3: ["z", "y", "x"],
        4: ["c", "z", "y", "x"],
        5: ["t", "c", "z", "y", "x"],
    }
    return names_by_ndim.get(len(shape), [f"dim_{i}" for i in range(len(shape))])


def _axis_type(axis: str) -> str:
    if axis == "t":
        return "time"
    if axis == "c":
        return "channel"
    return "space"
