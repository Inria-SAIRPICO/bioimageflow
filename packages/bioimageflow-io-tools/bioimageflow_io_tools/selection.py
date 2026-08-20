"""Axis-aware image validation and selection tools."""

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
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)

from ._axes import (
    normalize_axis_order,
    normalize_requested_axes,
    select_axis_range,
    select_indices,
)
from ._raster import read_raster, read_scene, write_raster


class ValidateImageLayout(ProcessingTool):
    """Validate that a declared axis layout matches an image array."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Validate Image Layout"
    documentation = "Validate axis names, uniqueness, length, requirements, and sizes."
    category = Category.UTILITIES
    tags = ["io", "metadata", "validation", "layout"]
    environment = GENERAL_ENV

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
        layout: Annotated[
            str,
            GUIMeta(
                display_name="Layout",
                description="Declared axis order, such as YX, ZYX, CZYX, or TCZYX.",
                connectable=Connectable.NEVER,
            ),
        ]
        required_axes: Annotated[
            str,
            GUIMeta(
                display_name="Required axes",
                description="Axes that must be present in the declared layout.",
                connectable=Connectable.NEVER,
            ),
        ] = ""
        min_size: Annotated[
            int | None,
            GUIMeta(
                display_name="Minimum size",
                description="Optional minimum size for every declared axis.",
                min=0,
                step=1,
                connectable=Connectable.NEVER,
            ),
        ] = None

    class Outputs(IOModel):
        valid: Annotated[bool, GUIMeta(display_name="Valid")]
        axes: Annotated[str, GUIMeta(display_name="Axes")]
        shape: Annotated[list[int], GUIMeta(display_name="Shape")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        data = read_raster(arguments.input_image)
        shape = tuple(data.shape)
        layout = normalize_axis_order(arguments.layout, shape)
        required_axes = normalize_requested_axes(arguments.required_axes)
        for axis in required_axes:
            if axis not in layout:
                raise ValueError(f"Axis order {layout!r} requires axis {axis!r}.")
        if arguments.min_size is not None:
            if arguments.min_size < 0:
                raise ValueError("min_size must be non-negative.")
            for axis, size in zip(layout, shape, strict=True):
                if size < arguments.min_size:
                    raise ValueError(
                        f"Axis {axis!r} has size {size}, below minimum "
                        f"{arguments.min_size}."
                    )
        return self.Outputs(valid=True, axes=layout, shape=list(shape))


class SelectScene(ProcessingTool):
    """Extract one scene from an ordinary or TIFF-series image."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Select Scene"
    documentation = "Extract scene 0 from ordinary images or a TIFF series by index."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "scene-selection"]
    environment = GENERAL_ENV

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
        scene: Annotated[
            int,
            GUIMeta(display_name="Scene", description="Zero-based scene index.", min=0, step=1),
        ] = 0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(display_name="Selected scene"),
        ] = Template("{input_image.stem}_scene_{scene}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = write_raster(
            read_scene(arguments.input_image, arguments.scene), arguments.output_image
        )
        return self.Outputs(output_image=output_path)


class SelectDimensions(ProcessingTool):
    """Select channel, Z, and/or time indices using an explicit axis order."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Select Dimensions"
    documentation = "Select validated C, Z, and T indices from a declared image layout."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection"]
    environment = GENERAL_ENV

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
                description="Image to slice using the declared layout.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        layout: Annotated[
            str,
            GUIMeta(
                display_name="Layout",
                description="Axis order for the input image.",
                connectable=Connectable.NEVER,
            ),
        ] = "CZYX"
        channel: Annotated[int | None, GUIMeta(display_name="Channel", min=0, step=1)] = None
        z: Annotated[int | None, GUIMeta(display_name="Z", min=0, step=1)] = None
        timepoint: Annotated[int | None, GUIMeta(display_name="Timepoint", min=0, step=1)] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(display_name="Selected image"),
        ] = Template("{input_image.stem}_selected{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        selected, remaining_axes = select_indices(
            read_raster(arguments.input_image),
            axis_order=arguments.layout,
            selections={"C": arguments.channel, "Z": arguments.z, "T": arguments.timepoint},
        )
        output_path = write_raster(
            selected, arguments.output_image, axes=remaining_axes
        )
        return self.Outputs(output_image=output_path)


class SelectTimepoint(ProcessingTool):
    """Select one timepoint from an image using a declared layout."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Select Timepoint"
    documentation = "Select one validated zero-based T index from a declared layout."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection", "time"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(), GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT)]
        layout: Annotated[str, GUIMeta(display_name="Layout", connectable=Connectable.NEVER)]
        timepoint: Annotated[int, GUIMeta(display_name="Timepoint", min=0, step=1)] = 0

    class Outputs(IOModel):
        output_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(display_name="Selected timepoint")] = Template("{input_image.stem}_t{timepoint}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        selected, remaining_axes = select_indices(
            read_raster(arguments.input_image),
            axis_order=arguments.layout,
            selections={"T": arguments.timepoint},
        )
        return self.Outputs(
            output_image=write_raster(
                selected, arguments.output_image, axes=remaining_axes
            )
        )


class SelectChannel(ProcessingTool):
    """Select one channel from an image using a declared layout."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Select Channel"
    documentation = "Select one validated zero-based C index from a declared layout."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection", "channel"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(), GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT)]
        layout: Annotated[str, GUIMeta(display_name="Layout", connectable=Connectable.NEVER)]
        channel: Annotated[int, GUIMeta(display_name="Channel", min=0, step=1)] = 0

    class Outputs(IOModel):
        output_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(display_name="Selected channel")] = Template("{input_image.stem}_c{channel}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        selected, remaining_axes = select_indices(
            read_raster(arguments.input_image),
            axis_order=arguments.layout,
            selections={"C": arguments.channel},
        )
        return self.Outputs(
            output_image=write_raster(
                selected, arguments.output_image, axes=remaining_axes
            )
        )


class SelectZRange(ProcessingTool):
    """Select a non-empty Z slab using explicit half-open bounds."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Select Z Range"
    documentation = "Select an in-bounds, start-inclusive, stop-exclusive Z range."
    category = Category.IMAGE_PROCESSING
    tags = ["io", "dimension-selection", "z"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(), GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT)]
        layout: Annotated[str, GUIMeta(display_name="Layout", connectable=Connectable.NEVER)]
        start_z: Annotated[int, GUIMeta(display_name="Start Z", min=0, step=1)] = 0
        stop_z: Annotated[int | None, GUIMeta(display_name="Stop Z", min=0, step=1)] = None

    class Outputs(IOModel):
        output_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(display_name="Selected Z range")] = Template("{input_image.stem}_z{start_z}_{stop_z}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        selected, remaining_axes = select_axis_range(
            read_raster(arguments.input_image),
            axis_order=arguments.layout,
            axis="Z",
            start=arguments.start_z,
            stop=arguments.stop_z,
        )
        return self.Outputs(
            output_image=write_raster(
                selected, arguments.output_image, axes=remaining_axes
            )
        )
