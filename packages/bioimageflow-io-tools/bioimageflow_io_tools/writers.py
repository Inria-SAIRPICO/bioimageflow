"""Ordinary raster, OME-TIFF, and OME-Zarr conversion tools."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    ImageSpec,
    IOModel,
    ProcessingTool,
    RowConsumption,
    Template,
)

from ._axes import normalize_axis_order, select_indices
from ._raster import read_raster, read_scene, write_raster
from .environments import bioio_env
from .metadata import inspect_image, usable_axis_order


class ConvertImageFormat(ProcessingTool):
    """Convert between ordinary raster formats, with optional slicing."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Convert Image Format"
    documentation = (
        "Convert an ordinary image through imageio, optionally selecting a TIFF scene "
        "or declared channel, Z plane, and timepoint first."
    )
    category = Category.CONVERSION
    tags = ["io", "conversion", "format-conversion"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT),
        ]
        input_layout: Annotated[
            str | None,
            GUIMeta(
                display_name="Input layout",
                description="Source axis order required for C, Z, or T selection.",
                connectable=Connectable.NEVER,
            ),
        ] = None
        scene: Annotated[int | None, GUIMeta(display_name="Scene", min=0, step=1)] = None
        channel: Annotated[int | None, GUIMeta(display_name="Channel", min=0, step=1)] = None
        z: Annotated[int | None, GUIMeta(display_name="Z slice", min=0, step=1)] = None
        timepoint: Annotated[int | None, GUIMeta(display_name="Timepoint", min=0, step=1)] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Converted image path",
                description="Destination in an ordinary image format.",
            ),
        ] = Template("{input_image.stem}_converted{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = (
            read_scene(arguments.input_image, arguments.scene)
            if arguments.scene is not None
            else read_raster(arguments.input_image)
        )
        selections = {
            "C": arguments.channel,
            "Z": arguments.z,
            "T": arguments.timepoint,
        }
        if any(index is not None for index in selections.values()):
            if arguments.input_layout is None:
                raise ValueError("input_layout is required for C, Z, or T selection.")
            data, _ = select_indices(
                data,
                axis_order=arguments.input_layout,
                selections=selections,
            )
        output_path = write_raster(data, arguments.output_image)
        return self.Outputs(output_image=output_path)


class ConvertToOmeTiff(ProcessingTool):
    """Convert an image file to OME-TIFF using explicit or reader-provided axes."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Convert to OME-TIFF"
    documentation = "Convert an image to OME-TIFF without inventing ambiguous axes."
    category = Category.CONVERSION
    tags = ["io", "conversion", "ome-tiff"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT),
        ]
        dimension_order: Annotated[
            str | None,
            GUIMeta(
                display_name="Dimension order",
                description="OME axes; required when source metadata is ambiguous.",
                connectable=Connectable.NEVER,
            ),
        ] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(formats={"ome-tiff"}),
            GUIMeta(display_name="OME-TIFF image"),
        ] = Template("{input_image.stem}.ome.tiff")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = read_raster(arguments.input_image)
        axes = _resolve_axes(
            arguments.input_image,
            tuple(data.shape),
            getattr(arguments, "dimension_order", None),
        )
        output_path = write_ome_tiff(data, arguments.output_image, axes)
        return self.Outputs(output_image=output_path)


class ConvertToOmeZarr(ProcessingTool):
    """Convert an image file with the maintained BioIO OME-Zarr writer."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Convert to OME-Zarr"
    documentation = (
        "Write a single-scale OME-Zarr v2 store with explicit or reader-provided axes "
        "and verify it by reopening through BioIO."
    )
    category = Category.CONVERSION
    tags = ["io", "conversion", "ome-zarr", "zarr", "bioio"]
    environment = bioio_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT),
        ]
        dimension_order: Annotated[
            str | None,
            GUIMeta(
                display_name="Dimension order",
                description="OME axes; required when source metadata is ambiguous.",
                connectable=Connectable.NEVER,
            ),
        ] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(formats={"ome-zarr"}),
            GUIMeta(display_name="OME-Zarr path"),
        ] = Template("{input_image.stem}.ome.zarr")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = read_raster(arguments.input_image)
        axes = _resolve_axes(
            arguments.input_image,
            tuple(data.shape),
            getattr(arguments, "dimension_order", None),
        )
        output_path = write_ome_zarr(data, arguments.output_image, axes, verify=True)
        return self.Outputs(output_image=output_path)


def write_ome_tiff(data: Any, output_path: Path | str, axes: str) -> Path:
    import numpy as np
    import tifffile

    array = np.asarray(data)
    normalized_axes = normalize_axis_order(axes, tuple(array.shape))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    if normalized_axes.endswith("S"):
        kwargs["photometric"] = "rgb"
    tifffile.imwrite(
        path,
        array,
        ome=True,
        metadata={"axes": normalized_axes},
        **kwargs,
    )
    return path


def write_ome_zarr(
    data: Any,
    output_path: Path | str,
    axes: str,
    *,
    verify: bool,
) -> Path:
    from bioio import BioImage  # type: ignore
    from bioio_ome_zarr.writers import OMEZarrWriter  # type: ignore
    import numpy as np

    array = np.asarray(data)
    normalized_axes = normalize_axis_order(axes, tuple(array.shape))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = OMEZarrWriter(
        store=str(path),
        level_shapes=array.shape,
        dtype=array.dtype,
        zarr_format=2,
        axes_names=[axis.lower() for axis in normalized_axes],
        axes_types=[_axis_type(axis) for axis in normalized_axes],
    )
    writer.write_full_volume(array)
    if verify:
        reopened = BioImage(path)
        reopened_data = reopened.get_image_dask_data(normalized_axes)
        if tuple(reopened_data.shape) != tuple(array.shape):
            raise RuntimeError(
                f"OME-Zarr verification failed: wrote shape {array.shape}, "
                f"reopened shape {tuple(reopened_data.shape)}."
            )
        if str(reopened_data.dtype) != str(array.dtype):
            raise RuntimeError(
                f"OME-Zarr verification failed: wrote dtype {array.dtype}, "
                f"reopened dtype {reopened_data.dtype}."
            )
    return path


def _resolve_axes(
    input_image: Path | str,
    shape: tuple[int, ...],
    explicit_axes: str | None,
) -> str:
    if explicit_axes is not None:
        return normalize_axis_order(explicit_axes, shape)
    metadata_axes = usable_axis_order(inspect_image(input_image))
    if metadata_axes is None:
        raise ValueError(
            "Source axes are ambiguous; set dimension_order explicitly before OME conversion."
        )
    return normalize_axis_order(metadata_axes, shape)


def _axis_type(axis: str) -> str:
    if axis == "T":
        return "time"
    if axis in {"C", "S"}:
        return "channel"
    return "space"
