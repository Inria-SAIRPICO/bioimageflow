"""Lightweight image IO tools."""

import json
from pathlib import Path
from typing import Annotated, Any
import xml.etree.ElementTree as ET

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
        output_path = Path(arguments.output_image)
        data = _read_image(arguments.input_image)
        _write_image(data, output_path)
        return self.Outputs(output_image=output_path)


class ReadImageMetadata(ProcessingTool):
    """Read lightweight image metadata without materializing workflow outputs."""

    display_name = "Read Image Metadata"
    documentation = "Report image shape, dtype, dimensionality, and a pragmatic axes guess."
    category = Category.CONVERSION
    tags = ["io", "metadata", "image"]
    environment = LIGHTWEIGHT_IO_ENV

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
        axes: Annotated[str, GUIMeta(display_name="Axes guess")]
        channel_names: Annotated[list[str], GUIMeta(display_name="Channel names")]
        pixel_sizes: Annotated[dict[str, float | None], GUIMeta(display_name="Pixel sizes")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_image(arguments.input_image)
        axes = _axes_guess_for_shape(data.shape)
        return self.Outputs(
            shape=list(data.shape),
            dtype=str(data.dtype),
            ndim=data.ndim,
            axes=axes,
            channel_names=_channel_names_for_axes(data.shape, axes),
            pixel_sizes=_pixel_sizes_from_metadata(arguments.input_image),
        )


class ValidateImageLayout(ProcessingTool):
    """Validate that a declared axis layout matches an image array."""

    display_name = "Validate Image Layout"
    documentation = "Validate layout length, required axes, and optional minimum axis sizes."
    category = Category.UTILITIES
    tags = ["io", "metadata", "validation", "layout"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image file whose declared layout should be validated.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        layout: Annotated[str, GUIMeta(
            display_name="Layout",
            description="Declared axis order, such as YX, ZYX, CZYX, TCYX, or TCZYX.",
            connectable=Connectable.NEVER,
        )]
        required_axes: Annotated[str, GUIMeta(
            display_name="Required axes",
            description="Axes that must be present in the declared layout.",
            connectable=Connectable.NEVER,
        )] = ""
        min_size: Annotated[int | None, GUIMeta(
            display_name="Minimum size",
            description="Optional minimum size for every declared axis.",
            min=0,
            step=1,
            connectable=Connectable.NEVER,
        )] = None

    class Outputs(IOModel):
        valid: Annotated[bool, GUIMeta(display_name="Valid")]
        axes: Annotated[str, GUIMeta(display_name="Axes")]
        shape: Annotated[list[int], GUIMeta(display_name="Shape")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_image(arguments.input_image)
        layout = _validate_layout_for_data(data, arguments.layout)
        required_axes = str(arguments.required_axes or "").upper()
        for axis in required_axes:
            if axis not in layout:
                raise ValueError(f"Layout {layout!r} requires axis {axis!r}.")
        if arguments.min_size is not None:
            for axis, size in zip(layout, data.shape, strict=True):
                if size < arguments.min_size:
                    raise ValueError(
                        f"Axis {axis!r} has size {size}, below minimum "
                        f"{arguments.min_size}."
                    )
        return self.Outputs(valid=True, axes=layout, shape=list(data.shape))


class ConvertImageFormat(ProcessingTool):
    """Convert an image to a path-selected output format, with optional slicing."""

    display_name = "Convert Image Format"
    documentation = (
        "Convert an image through imageio, OME-TIFF, or minimal OME-Zarr output. "
        "Optionally select a scene, channel, Z plane, or timepoint before export."
    )
    category = Category.CONVERSION
    tags = ["io", "conversion", "format-conversion", "ome-tiff", "ome-zarr"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image to convert.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        input_layout: Annotated[str | None, GUIMeta(
            display_name="Input layout",
            description="Optional source axis order for channel, Z, and timepoint selection.",
            connectable=Connectable.NEVER,
        )] = None
        scene: Annotated[int | None, GUIMeta(
            display_name="Scene",
            description="Optional zero-based TIFF scene index.",
            min=0,
            step=1,
            connectable=Connectable.NEVER,
        )] = None
        channel: Annotated[int | None, GUIMeta(
            display_name="Channel",
            description="Optional zero-based C index to export.",
            min=0,
            step=1,
            connectable=Connectable.NEVER,
        )] = None
        z: Annotated[int | None, GUIMeta(
            display_name="Z slice",
            description="Optional zero-based Z index to export.",
            min=0,
            step=1,
            connectable=Connectable.NEVER,
        )] = None
        timepoint: Annotated[int | None, GUIMeta(
            display_name="Timepoint",
            description="Optional zero-based T index to export.",
            min=0,
            step=1,
            connectable=Connectable.NEVER,
        )] = None
        dimension_order: Annotated[str | None, GUIMeta(
            display_name="Dimension order",
            description="Optional OME axis order for OME-TIFF outputs.",
            connectable=Connectable.NEVER,
        )] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Converted image path",
                description="Destination path. .ome.tiff/.ome.tif and .ome.zarr choose OME writers.",
            ),
        ] = Template("{input_image.stem}_converted{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_image_for_conversion(arguments)
        output_path = Path(arguments.output_image)
        _write_image(data, output_path, dimension_order=arguments.dimension_order)
        return self.Outputs(output_image=output_path)


class SelectScene(ProcessingTool):
    """Extract one image scene from an ordinary or TIFF-series image."""

    display_name = "Select Scene"
    documentation = "Extract scene 0 from ordinary images or a tifffile series by index."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "scene-selection"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image file or TIFF file with multiple series.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        scene: Annotated[int, GUIMeta(
            display_name="Scene",
            description="Zero-based scene index.",
            min=0,
            step=1,
        )] = 0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(display_name="Selected scene"),
        ] = Template("{input_image.stem}_scene_{scene}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_scene(arguments.input_image, arguments.scene)
        output_path = Path(arguments.output_image)
        _write_image(data, output_path)
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
        data = _read_image(arguments.input_image)
        selected = _select_dimensions(
            data,
            layout=arguments.layout,
            channel=arguments.channel,
            z=arguments.z,
            timepoint=arguments.timepoint,
        )
        output_path = Path(arguments.output_image)
        _write_image(selected, output_path)
        return self.Outputs(output_image=output_path)


class SelectTimepoint(ProcessingTool):
    """Select one timepoint from an image using a declared layout."""

    display_name = "Select Timepoint"
    documentation = "Select one zero-based T index from a declared image layout."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection", "time"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(), GUIMeta(
            display_name="Input image",
            description="Image to slice.",
            connectable=Connectable.BY_DEFAULT,
        )]
        layout: Annotated[str, GUIMeta(
            display_name="Layout",
            description="Declared axis order containing T.",
            connectable=Connectable.NEVER,
        )]
        timepoint: Annotated[int, GUIMeta(
            display_name="Timepoint",
            description="Zero-based T index.",
            min=0,
            step=1,
        )] = 0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(display_name="Selected timepoint"),
        ] = Template("{input_image.stem}_t{timepoint}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_image(arguments.input_image)
        selected = _select_dimensions(
            data,
            layout=arguments.layout,
            channel=None,
            z=None,
            timepoint=arguments.timepoint,
        )
        output_path = Path(arguments.output_image)
        _write_image(selected, output_path)
        return self.Outputs(output_image=output_path)


class SelectChannel(ProcessingTool):
    """Select one channel from an image using a declared layout."""

    display_name = "Select Channel"
    documentation = "Select one zero-based C index from a declared image layout."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection", "channel"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(), GUIMeta(
            display_name="Input image",
            description="Image to slice.",
            connectable=Connectable.BY_DEFAULT,
        )]
        layout: Annotated[str, GUIMeta(
            display_name="Layout",
            description="Declared axis order containing C.",
            connectable=Connectable.NEVER,
        )]
        channel: Annotated[int, GUIMeta(
            display_name="Channel",
            description="Zero-based C index.",
            min=0,
            step=1,
        )] = 0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(display_name="Selected channel"),
        ] = Template("{input_image.stem}_c{channel}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_image(arguments.input_image)
        selected = _select_dimensions(
            data,
            layout=arguments.layout,
            channel=arguments.channel,
            z=None,
            timepoint=None,
        )
        output_path = Path(arguments.output_image)
        _write_image(selected, output_path)
        return self.Outputs(output_image=output_path)


class SelectZRange(ProcessingTool):
    """Select a Z slab from an image using Python-style slice bounds."""

    display_name = "Select Z Range"
    documentation = "Select a start-inclusive, stop-exclusive Z range from a declared layout."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection", "z"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(), GUIMeta(
            display_name="Input image",
            description="Image to slice.",
            connectable=Connectable.BY_DEFAULT,
        )]
        layout: Annotated[str, GUIMeta(
            display_name="Layout",
            description="Declared axis order containing Z.",
            connectable=Connectable.NEVER,
        )]
        start_z: Annotated[int, GUIMeta(
            display_name="Start Z",
            description="Start-inclusive zero-based Z index.",
            min=0,
            step=1,
        )] = 0
        stop_z: Annotated[int | None, GUIMeta(
            display_name="Stop Z",
            description="Stop-exclusive Z index. Leave empty to keep through the end.",
            min=0,
            step=1,
        )] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(display_name="Selected Z range"),
        ] = Template("{input_image.stem}_z{start_z}_{stop_z}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = _read_image(arguments.input_image)
        selected = _select_z_range(
            data,
            layout=arguments.layout,
            start_z=arguments.start_z,
            stop_z=arguments.stop_z,
        )
        output_path = Path(arguments.output_image)
        _write_image(selected, output_path)
        return self.Outputs(output_image=output_path)


class ConvertToOmeTiff(ProcessingTool):
    """Convert an image file to OME-TIFF using tifffile."""

    display_name = "Convert to OME-TIFF"
    documentation = (
        "Read an image file and convert it to OME-TIFF with explicit OME axis metadata."
    )
    category = Category.CONVERSION
    tags = ["io", "conversion", "ome-tiff"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image file to convert to OME-TIFF.",
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
                description="Converted OME-TIFF image.",
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


class ConvertToOmeZarr(ProcessingTool):
    """Convert an image file to a minimal OME-NGFF/Zarr v2 directory."""

    display_name = "Convert to OME-Zarr"
    documentation = (
        "Read an image file and convert it to a single-scale uncompressed OME-Zarr v2 directory."
    )
    category = Category.CONVERSION
    tags = ["io", "conversion", "ome-zarr", "zarr"]
    environment = LIGHTWEIGHT_IO_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(
                display_name="Input image",
                description="Image file to convert to OME-Zarr.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(formats={"ome-zarr"}),
            GUIMeta(
                display_name="OME-Zarr path",
                description="Converted OME-Zarr directory.",
            ),
        ] = Template("{input_image.stem}.ome.zarr")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        output_path = Path(arguments.output_image)
        data = iio.imread(arguments.input_image)
        _write_minimal_ome_zarr(data, output_path)
        return self.Outputs(output_image=output_path)


def _read_image_for_conversion(arguments: Arguments) -> Any:
    data = (
        _read_scene(arguments.input_image, arguments.scene)
        if arguments.scene is not None
        else _read_image(arguments.input_image)
    )
    if any(
        selection is not None
        for selection in (arguments.channel, arguments.z, arguments.timepoint)
    ):
        if arguments.input_layout is None:
            raise ValueError(
                "input_layout is required when selecting channel, z, or timepoint."
            )
        data = _select_dimensions(
            data,
            layout=arguments.input_layout,
            channel=arguments.channel,
            z=arguments.z,
            timepoint=arguments.timepoint,
        )
    return data


def _select_dimensions(
    data: Any,
    *,
    layout: str,
    channel: int | None,
    z: int | None,
    timepoint: int | None,
) -> Any:
    normalized_layout = _validate_layout_for_data(data, layout)

    selectors: list[int | slice] = [slice(None)] * data.ndim
    for axis, index in {"T": timepoint, "C": channel, "Z": z}.items():
        if index is None:
            continue
        if axis not in normalized_layout:
            raise ValueError(f"Cannot select {axis}: layout {layout!r} has no {axis} axis.")
        selectors[normalized_layout.index(axis)] = index
    return data[tuple(selectors)]


def _select_z_range(
    data: Any,
    *,
    layout: str,
    start_z: int,
    stop_z: int | None,
) -> Any:
    normalized_layout = _validate_layout_for_data(data, layout)
    if "Z" not in normalized_layout:
        raise ValueError(f"Cannot select Z: layout {layout!r} has no Z axis.")
    if stop_z is not None and stop_z < start_z:
        raise ValueError("stop_z must be greater than or equal to start_z.")
    selectors: list[int | slice] = [slice(None)] * data.ndim
    selectors[normalized_layout.index("Z")] = slice(start_z, stop_z)
    return data[tuple(selectors)]


def _validate_layout_for_data(data: Any, layout: str) -> str:
    normalized_layout = layout.upper()
    allowed_axes = {"T", "C", "Z", "Y", "X"}
    unknown_axes = sorted(set(normalized_layout) - allowed_axes)
    if unknown_axes:
        raise ValueError(
            f"Layout {layout!r} contains unknown axes: {''.join(unknown_axes)}."
        )
    if len(normalized_layout) != data.ndim:
        raise ValueError(
            f"Layout {layout!r} has {len(normalized_layout)} axes, "
            f"but image has {data.ndim} dimensions."
        )
    if len(set(normalized_layout)) != len(normalized_layout):
        raise ValueError(f"Layout {layout!r} contains duplicate axes.")
    return normalized_layout


def _read_image(input_image: Path | str) -> Any:
    import imageio.v3 as iio

    return iio.imread(input_image)


def _channel_names_for_axes(shape: tuple[int, ...], axes: str) -> list[str]:
    if "C" not in axes:
        return []
    channel_count = int(shape[axes.index("C")])
    return [f"channel_{index}" for index in range(channel_count)]


def _pixel_sizes_from_metadata(input_image: Path | str) -> dict[str, float | None]:
    pixel_sizes: dict[str, float | None] = {"X": None, "Y": None, "Z": None}
    path = Path(input_image)
    if not _is_tiff_path(path):
        return pixel_sizes

    try:
        import tifffile

        with tifffile.TiffFile(path) as tif:
            if tif.ome_metadata:
                return _pixel_sizes_from_ome_xml(tif.ome_metadata)
            series = tif.series[0] if tif.series else None
            if series is not None and getattr(series, "levels", None):
                metadata = getattr(series.levels[0], "metadata", None) or {}
            else:
                metadata = {}
            physical_size_x = metadata.get("PhysicalSizeX")
            physical_size_y = metadata.get("PhysicalSizeY")
            physical_size_z = metadata.get("PhysicalSizeZ")
            pixel_sizes["X"] = float(physical_size_x) if physical_size_x else None
            pixel_sizes["Y"] = float(physical_size_y) if physical_size_y else None
            pixel_sizes["Z"] = float(physical_size_z) if physical_size_z else None
    except Exception:
        return pixel_sizes
    return pixel_sizes


def _pixel_sizes_from_ome_xml(ome_xml: str) -> dict[str, float | None]:
    pixel_sizes: dict[str, float | None] = {"X": None, "Y": None, "Z": None}
    root = ET.fromstring(ome_xml)
    pixels = root.find(".//{*}Pixels")
    if pixels is None:
        return pixel_sizes
    for axis in pixel_sizes:
        value = pixels.attrib.get(f"PhysicalSize{axis}")
        pixel_sizes[axis] = float(value) if value else None
    return pixel_sizes


def _read_scene(input_image: Path | str, scene: int) -> Any:
    import imageio.v3 as iio

    path = Path(input_image)
    if scene < 0:
        raise IndexError(f"Scene index {scene} is invalid; scene indexes are zero-based.")

    if _is_tiff_path(path):
        import tifffile

        with tifffile.TiffFile(path) as tif:
            if scene < len(tif.series):
                return tif.series[scene].asarray()
            if scene != 0:
                raise IndexError(
                    f"Scene index {scene} is out of range for {len(tif.series)} series."
                )

    if scene == 0:
        return iio.imread(path)
    raise IndexError(f"Scene index {scene} is only available for multi-series TIFF files.")


def _write_image(data: Any, output_path: Path, *, dimension_order: str | None = None) -> None:
    import tifffile

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_ome_zarr_path(output_path):
        _write_minimal_ome_zarr(data, output_path)
    elif _is_ome_tiff_path(output_path):
        axes = _dimension_order_for_shape(data.shape, dimension_order)
        tifffile.imwrite(output_path, data, ome=True, metadata={"axes": axes})
    else:
        _write_tiff_safe_image(output_path, data)


def _write_tiff_safe_image(output_path: Path, data: Any) -> None:
    import imageio.v3 as iio
    import numpy as np

    array = np.asarray(data)
    kwargs: dict[str, str] = {}
    if _is_tiff_path(output_path) and _is_grayscale_stack(array):
        kwargs["photometric"] = "minisblack"
    iio.imwrite(output_path, array, **kwargs)


def _is_grayscale_stack(array: Any) -> bool:
    if array.ndim < 3:
        return False
    if array.ndim == 3 and array.shape[-1] in {3, 4}:
        return False
    return True


def _is_tiff_path(path: Path) -> bool:
    return path.suffix.lower() in {".tif", ".tiff"}


def _is_ome_tiff_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".ome.tif") or name.endswith(".ome.tiff")


def _is_ome_zarr_path(path: Path) -> bool:
    return path.name.lower().endswith(".ome.zarr")


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


def _axes_guess_for_shape(shape: tuple[int, ...]) -> str:
    default_by_ndim = {
        2: "YX",
        3: "ZYX",
        4: "CZYX",
        5: "TCZYX",
    }
    return default_by_ndim.get(len(shape), ",".join(f"D{i}" for i in range(len(shape))))


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
