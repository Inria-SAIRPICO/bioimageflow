"""Broad plugin-backed image conversion through BioIO."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GUIMeta,
    ImageSpec,
    IOModel,
    ProcessingTool,
    RowConsumption,
    Template,
)

from ._axes import (
    normalize_axis_order,
    remaining_axis_order,
    validate_unbound_axis_order,
)
from ._raster import is_grayscale_stack, is_ome_tiff_path, is_ome_zarr_path, is_tiff_path
from .environments import bioio_env
from .writers import write_ome_zarr


class BioIOConvertImage(ProcessingTool):
    """Convert broad microscopy formats through BioIO reader and writer plugins."""

    row_consumption = RowConsumption.MAPPED
    display_name = "BioIO Convert Image"
    documentation = (
        "Convert plugin-backed microscopy formats with explicit output axes and "
        "axis-aware scene, channel, Z, and time selection."
    )
    category = Category.CONVERSION
    tags = ["format conversion", "bioio"]
    environment = bioio_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT),
        ]
        dim_order: Annotated[
            str,
            GUIMeta(
                display_name="Dimension order",
                description="Requested output axes before explicitly selected dimensions are removed.",
            ),
        ] = "TCZYX"
        scene: Annotated[int | None, GUIMeta(display_name="Scene", min=0, step=1)] = None
        channel: Annotated[int | None, GUIMeta(display_name="Channel", min=0, step=1)] = None
        z: Annotated[int | None, GUIMeta(display_name="Z slice", min=0, step=1)] = None
        timepoint: Annotated[int | None, GUIMeta(display_name="Timepoint", min=0, step=1)] = None

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(),
            GUIMeta(display_name="Output image"),
        ] = Template("{input_image.stem}.ome.tiff")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from bioio import BioImage  # type: ignore

        input_path = Path(arguments.input_image)
        output_path = Path(arguments.output_image)
        requested_order = validate_unbound_axis_order(arguments.dim_order)
        image = BioImage(input_path)
        if arguments.scene is not None:
            _validate_scene(image, arguments.scene)
            image.set_scene(arguments.scene)

        selections = {
            "C": arguments.channel,
            "Z": arguments.z,
            "T": arguments.timepoint,
        }
        selected_axes = [axis for axis, index in selections.items() if index is not None]
        output_order = remaining_axis_order(requested_order, selected_axes)
        dim_kwargs = {
            axis: _validate_bioio_index(image, axis, index)
            for axis, index in selections.items()
            if index is not None
        }
        data = image.get_image_data(output_order, **dim_kwargs)
        output_order = normalize_axis_order(output_order, tuple(data.shape))
        _write_bioio_output(data, output_path, output_order)
        return self.Outputs(output_image=output_path)


def _validate_scene(image: Any, scene: int) -> None:
    if scene < 0:
        raise IndexError(f"Scene index {scene} is invalid; indexes are zero-based.")
    scenes = getattr(image, "scenes", None)
    if scenes is not None and scene >= len(scenes):
        raise IndexError(f"Scene index {scene} is out of range for {len(scenes)} scenes.")


def _validate_bioio_index(image: Any, axis: str, index: int) -> int:
    if index < 0:
        raise IndexError(f"{axis} index {index} is invalid; indexes are zero-based.")
    size = getattr(getattr(image, "dims", None), axis, None)
    if isinstance(size, int) and index >= size:
        raise IndexError(f"{axis} index {index} is out of range for axis size {size}.")
    return index


def _write_bioio_output(data: Any, output_path: Path, axes: str) -> None:
    from bioio_imageio.writers import TwoDWriter  # type: ignore
    from bioio_ome_tiff.writers import OmeTiffWriter  # type: ignore
    import numpy as np
    import tifffile

    array = np.asarray(data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if is_ome_tiff_path(output_path):
        OmeTiffWriter.save(array, str(output_path), dim_order=axes)
    elif is_ome_zarr_path(output_path):
        write_ome_zarr(array, output_path, axes, verify=True)
    elif is_tiff_path(output_path):
        kwargs: dict[str, Any] = (
            {"photometric": "minisblack"} if is_grayscale_stack(array) else {}
        )
        tifffile.imwrite(output_path, array, **kwargs)
    elif axes in {"YX", "YXS", "SYX"}:
        TwoDWriter.save(array, str(output_path), dim_order=axes)
    else:
        raise ValueError(
            f"Cannot write axes {axes!r} to ordinary 2D format {output_path.suffix!r}; "
            "select remaining T, C, and Z dimensions or choose an OME output."
        )
