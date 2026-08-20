"""Image metadata inspection tools and format-specific readers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
import xml.etree.ElementTree as ET

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
)

from ._raster import is_tiff_path


@dataclass(frozen=True)
class ImageMetadata:
    """Metadata available without reading an image's full pixel array."""

    shape: tuple[int, ...]
    dtype: str
    axes: str
    channel_names: tuple[str, ...]
    pixel_sizes: dict[str, float | None]


class ReadImageMetadata(ProcessingTool):
    """Read image metadata without loading the full pixel array when supported."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Read Image Metadata"
    documentation = (
        "Report reader-provided shape, dtype, axes, channels, and physical pixel sizes. "
        "Unknown axes are reported as '?'."
    )
    category = Category.CONVERSION
    tags = ["io", "metadata", "image"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image file to inspect.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        shape: Annotated[list[int], GUIMeta(display_name="Shape")]
        dtype: Annotated[str, GUIMeta(display_name="Dtype")]
        ndim: Annotated[int, GUIMeta(display_name="Dimensions")]
        axes: Annotated[str, GUIMeta(display_name="Axes")]
        channel_names: Annotated[list[str], GUIMeta(display_name="Channel names")]
        pixel_sizes: Annotated[dict[str, float | None], GUIMeta(display_name="Pixel sizes")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        metadata = inspect_image(arguments.input_image)
        return self.Outputs(
            shape=list(metadata.shape),
            dtype=metadata.dtype,
            ndim=len(metadata.shape),
            axes=metadata.axes,
            channel_names=list(metadata.channel_names),
            pixel_sizes=metadata.pixel_sizes,
        )


def inspect_image(input_image: Path | str) -> ImageMetadata:
    """Inspect a TIFF directly or fall back to ImageIO header properties."""
    path = Path(input_image)
    if is_tiff_path(path):
        return _inspect_tiff(path)
    return _inspect_imageio(path)


def usable_axis_order(metadata: ImageMetadata) -> str | None:
    """Return metadata axes only when every dimension is unambiguous."""
    return metadata.axes if "?" not in metadata.axes else None


def _inspect_tiff(path: Path) -> ImageMetadata:
    import tifffile

    with tifffile.TiffFile(path) as tif:
        if not tif.series:
            raise ValueError(f"TIFF image {path} contains no image series.")
        series = tif.series[0]
        shape = tuple(int(size) for size in series.shape)
        axes = _reported_axes(str(series.axes), shape)
        pixel_sizes = _empty_pixel_sizes()
        channel_names: tuple[str, ...] = ()
        if tif.ome_metadata:
            pixel_sizes, ome_channel_names = _parse_ome_xml(tif.ome_metadata)
            channel_names = tuple(ome_channel_names)
        if not channel_names:
            channel_names = _default_channel_names(shape, axes)
        return ImageMetadata(
            shape=shape,
            dtype=str(series.dtype),
            axes=axes,
            channel_names=channel_names,
            pixel_sizes=pixel_sizes,
        )


def _inspect_imageio(path: Path) -> ImageMetadata:
    import imageio.v3 as iio
    import numpy as np

    properties = iio.improps(path)
    shape = tuple(int(size) for size in properties.shape)
    metadata = iio.immeta(path)
    reader_axes = metadata.get("axes") if hasattr(metadata, "get") else None
    axes = (
        _reported_axes(str(reader_axes), shape)
        if isinstance(reader_axes, str)
        else _conservative_axes(shape)
    )
    return ImageMetadata(
        shape=shape,
        dtype=str(np.dtype(properties.dtype)),
        axes=axes,
        channel_names=_default_channel_names(shape, axes),
        pixel_sizes=_empty_pixel_sizes(),
    )


def _reported_axes(reader_axes: str, shape: tuple[int, ...]) -> str:
    if len(reader_axes) != len(shape):
        return _conservative_axes(shape)
    known = {"T", "C", "Z", "Y", "X", "S"}
    normalized = "".join(axis if axis in known else "?" for axis in reader_axes.upper())
    if normalized.endswith("YX?") and shape[-1] in {3, 4}:
        normalized = f"{normalized[:-1]}S"
    return normalized


def _conservative_axes(shape: tuple[int, ...]) -> str:
    if len(shape) == 2:
        return "YX"
    if len(shape) == 3 and shape[-1] in {3, 4}:
        return "YXS"
    if len(shape) >= 2:
        return f"{'?' * (len(shape) - 2)}YX"
    return "?" * len(shape)


def _default_channel_names(shape: tuple[int, ...], axes: str) -> tuple[str, ...]:
    if "C" in axes:
        return tuple(f"channel_{index}" for index in range(shape[axes.index("C")]))
    if "S" in axes:
        sample_count = shape[axes.index("S")]
        return ("red", "green", "blue", "alpha")[:sample_count]
    return ()


def _empty_pixel_sizes() -> dict[str, float | None]:
    return {"X": None, "Y": None, "Z": None}


def _parse_ome_xml(
    ome_xml: str,
) -> tuple[dict[str, float | None], list[str]]:
    pixel_sizes = _empty_pixel_sizes()
    root = ET.fromstring(ome_xml)
    pixels = root.find(".//{*}Pixels")
    if pixels is None:
        return pixel_sizes, []
    for axis in pixel_sizes:
        value = pixels.attrib.get(f"PhysicalSize{axis}")
        pixel_sizes[axis] = float(value) if value is not None else None
    channel_names = []
    for index, channel in enumerate(pixels.findall("{*}Channel")):
        channel_names.append(channel.attrib.get("Name") or f"channel_{index}")
    return pixel_sizes, channel_names
