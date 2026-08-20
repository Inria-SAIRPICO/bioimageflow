"""Mosaic — aggregate multiple images into a single grid montage."""

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
    SCALAR_IMAGE_SEMANTICS,
    Template,
)
from bioimageflow_core.types import Layout, Semantic


class Mosaic(ProcessingTool):
    """Create a mosaic (grid) from all input images.

    Uses ``process_batch`` to collect every row's image and assemble them
    into a single composite grid.  Each input row receives the same output
    (the mosaic path and total image count).
    """
    row_consumption = RowConsumption.COLLECTIVE
    display_name = "Mosaic"
    documentation = (
        "Aggregates images into a single mosaic image arranged in a grid. "
        "Each input row receives the mosaic path and the total image count."
    )
    category = Category.UTILITIES
    tags = ["visualization", "aggregation"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics=SCALAR_IMAGE_SEMANTICS),
            GUIMeta(
                display_name="Input image",
                description="Scalar image tile to include in the mosaic. One row per tile.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        columns: Annotated[int, GUIMeta(
            display_name="Columns",
            description="Number of tiles per row in the output grid.",
            min=1, max=100, step=1,
        )] = 5
        tile_width: Annotated[int | None, GUIMeta(
            display_name="Tile width",
            description="Resize each tile to this width in pixels. Leave empty to keep the original width.",
            min=1, step=1,
        )] = None
        tile_height: Annotated[int | None, GUIMeta(
            display_name="Tile height",
            description="Resize each tile to this height in pixels. Leave empty to keep the original height.",
            min=1, step=1,
        )] = None

    class Outputs(IOModel):
        
        mosaic_path: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR, Layout.PLANAR_CHANNEL},
            ),
            GUIMeta(
            display_name="Mosaic image",
            description="Composite mosaic image (grid of all input tiles).",
            ),
        ] = Template("{node_name}_mosaic.png")
        image_count: Annotated[int, GUIMeta(
            display_name="Image count",
            description="Number of input tiles assembled in the mosaic.",
        )]

    def process_batch(
        self,
        arguments_list: list[Arguments],
        *,
        context: Any = None,
    ) -> Any:
        from PIL import Image

        if not arguments_list:
            return []

        first = arguments_list[0]
        columns = int(first.columns)
        tile_width = first.tile_width
        tile_height = first.tile_height
        if columns < 1:
            raise ValueError("Columns must be at least 1.")
        if tile_width is not None and tile_width < 1:
            raise ValueError("Tile width must be at least 1 when provided.")
        if tile_height is not None and tile_height < 1:
            raise ValueError("Tile height must be at least 1 when provided.")
        for args in arguments_list[1:]:
            settings = (int(args.columns), args.tile_width, args.tile_height)
            if settings != (columns, tile_width, tile_height):
                raise ValueError("Mosaic layout settings must be identical for every row.")

        images = []
        try:
            for args in arguments_list:
                with Image.open(str(args.input_image)) as source:
                    source.load()
                    image = source.copy()
                if tile_width is not None or tile_height is not None:
                    width = tile_width if tile_width is not None else image.size[0]
                    height = tile_height if tile_height is not None else image.size[1]
                    resized = image.resize((width, height))
                    image.close()
                    image = resized
                images.append(image)

            cols = min(columns, len(images))
            rows = (len(images) + cols - 1) // cols
            cell_width = max(image.width for image in images)
            cell_height = max(image.height for image in images)
            mode = _mosaic_mode(images)
            background: int | tuple[int, ...]
            background = (0, 0, 0, 0) if mode == "RGBA" else 0
            canvas = Image.new(
                mode,
                (cols * cell_width, rows * cell_height),
                color=background,
            )
            for idx, img in enumerate(images):
                x = (idx % cols) * cell_width
                y = (idx // cols) * cell_height
                tile = img if img.mode == mode else img.convert(mode)
                try:
                    if mode == "RGBA":
                        canvas.alpha_composite(tile, (x, y))
                    else:
                        canvas.paste(tile, (x, y))
                finally:
                    if tile is not img:
                        tile.close()

            output_path = Path(first.mosaic_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                canvas.save(str(output_path))
            finally:
                canvas.close()
        finally:
            for image in images:
                image.close()

        # Every input row maps to the same mosaic output (1-to-1).
        return [
            self.Outputs(mosaic_path=output_path, image_count=len(images))
            for _ in arguments_list
        ]


def _mosaic_mode(images: list[Any]) -> str:
    """Choose a PNG-compatible mode without discarding color or transparency."""
    modes = {image.mode for image in images}
    if modes & {"LA", "PA", "RGBA"}:
        return "RGBA"
    if not modes <= {"1", "L"}:
        return "RGB"
    return "L"
