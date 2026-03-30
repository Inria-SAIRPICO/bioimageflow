"""ConvertImage — convert image file formats using bioio."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    EnvironmentSpec,
    ImageSpec,
    IOModel,
    ProcessingTool,
)

bioio_env = EnvironmentSpec(
    name="bioio-all",
    dependencies={
        "python": "3.12",
        "pip": [
            "bioio==3.0.0",
            "pillow==11.1.0",
            "bioio-ome-zarr",
            "bioio-ome-tiff",
            "bioio-czi",
            "bioio-imageio",
            "bioio-tifffile",
            "bioio-tiff-glob",
        ],
    },
)


class ConvertImage(ProcessingTool):
    """Convert image file formats using bioio.

    Supports reading CZI, DV, PNG, GIF, LIF, ND2, OME-TIFF, OME-ZARR,
    SLDY, TIFF, and Bio-Formats files. The extension of the output file
    specifies the target format.
    """
    display_name = "Convert Image"
    documentation = (
        "Convert image file formats using bioio. The output format is "
        "determined by the output file extension."
    )
    category = Category.CONVERSION
    tags = ["format conversion", "bioio"]
    environment = bioio_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec()]
        dim_order: str = "TCZYX"
        scene: int | None = None
        channel: int | None = None
        z: int | None = None
        timepoint: int | None = None

    class Outputs(IOModel):
        output_image: Annotated[Path, ImageSpec()] = Path("{input_image.stem}.ome.tiff")

    def process_row(self, arguments: Arguments) -> Any:
        from bioio import BioImage                          #type: ignore
        from bioio_ome_tiff.writers import OmeTiffWriter    #type: ignore
        from bioio_ome_zarr.writers import OMEZarrWriter    #type: ignore
        from bioio_imageio.writers import TwoDWriter        #type: ignore
        import numpy as np
        import tifffile

        input_path = Path(arguments.input_image)
        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"Converting {input_path.name}...")
        image = BioImage(input_path)

        if arguments.scene is not None:
            image.set_scene(arguments.scene)

        # Show dimension sizes
        print(f"Image: {input_path.name}")
        print(f"  Dimensions: {image.dims}")
        print(f"  Shape: {image.shape}")
        print(f"  Dtype: {image.dtype}")

        dim_kwargs: dict[str, int] = {}
        if arguments.channel is not None:
            dim_kwargs["C"] = arguments.channel
        if arguments.z is not None:
            dim_kwargs["Z"] = arguments.z
        if arguments.timepoint is not None:
            dim_kwargs["T"] = arguments.timepoint

        # Use user-specified dim_order for reading
        dim_order = arguments.dim_order
        if dim_kwargs:
            data = image.get_image_data(dim_order, **dim_kwargs)
        else:
            data = image.get_image_data(dim_order)

        # Squeeze singleton leading dimensions (T, Z) so downstream tools
        # see CYX / YX shapes rather than TCZYX with size-1 axes.
        while data.ndim > 2 and data.shape[0] == 1:
            data = data[0]
            dim_order = dim_order[1:]

        print(f"  Output shape ({dim_order}): {data.shape}")

        suffixes = "".join(output_path.suffixes).lower()

        if suffixes.endswith(".ome.tiff") or suffixes.endswith(".ome.tif"):
            OmeTiffWriter.save(data, str(output_path), dim_order=dim_order)
        elif suffixes.endswith(".ome.zarr"):
            axes_map = {
                "T": ("t", "time"), "C": ("c", "channel"), "Z": ("z", "space"),
                "Y": ("y", "space"), "X": ("x", "space"), "S": ("s", "channel"),
            }
            axes_names = [axes_map[c][0] for c in dim_order]
            axes_types = [axes_map[c][1] for c in dim_order]
            writer = OMEZarrWriter(
                store=str(output_path),
                level_shapes=data.shape,
                dtype=data.dtype,
                axes_names=axes_names,
                axes_types=axes_types,
            )
            writer.write_full_volume(data)
        elif suffixes.endswith(".tiff") or suffixes.endswith(".tif"):
            tifffile.imwrite(str(output_path), data)
        else:
            img_2d = np.squeeze(data)
            if img_2d.ndim > 3:
                raise ValueError(
                    f"Cannot save {img_2d.ndim}D data to {output_path.suffix}. "
                    f"Use dimension selection (channel, z, timepoint) to reduce."
                )
            if img_2d.ndim == 3 and img_2d.shape[0] in (1, 3, 4):
                TwoDWriter.save(img_2d, str(output_path), "SYX")
            else:
                TwoDWriter.save(img_2d, str(output_path))

        return self.Outputs(output_image=output_path)
