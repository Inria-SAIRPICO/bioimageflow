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
    Semantic,
)


class Mosaic(ProcessingTool):
    """Create a mosaic (grid) from all input images.

    Uses ``process_batch`` to collect every row's image and assemble them
    into a single composite grid.  Each input row receives the same output
    (the mosaic path and total image count).
    """
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
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(
                display_text="Input image",
                description="Image tile to include in the mosaic. One row per tile.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        columns: Annotated[int, GUIMeta(
            display_text="Columns",
            description="Number of tiles per row in the output grid.",
            min=1, max=100, step=1,
        )] = 5
        tile_width: Annotated[int | None, GUIMeta(
            display_text="Tile width",
            description="Resize each tile to this width in pixels. Leave empty to keep the original width.",
            min=1, step=1,
        )] = None
        tile_height: Annotated[int | None, GUIMeta(
            display_text="Tile height",
            description="Resize each tile to this height in pixels. Leave empty to keep the original height.",
            min=1, step=1,
        )] = None

    class Outputs(IOModel):
        mosaic_path: Annotated[Path, GUIMeta(
            display_text="Mosaic image",
            description="Composite mosaic image (grid of all input tiles).",
        )] = Path("{node_name}_mosaic.png")
        image_count: Annotated[int, GUIMeta(
            display_text="Image count",
            description="Number of input tiles assembled in the mosaic.",
        )]

    def process_batch(self, arguments_list: list[Arguments]) -> Any:
        from PIL import Image

        images = []
        for args in arguments_list:
            img = Image.open(str(args.input_image))
            if args.tile_width is not None or args.tile_height is not None:
                width = args.tile_width if args.tile_width is not None else img.size[0]
                height = args.tile_height if args.tile_height is not None else img.size[1]
                img = img.resize((width, height))
            images.append(img)

        # Use the output path from the first row (all rows share the same
        # template since it only uses {node_name}).
        output_path = Path(arguments_list[0].mosaic_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cols = arguments_list[0].columns
        if not images:
            canvas = Image.new("RGB", (1, 1))
        else:
            rows = (len(images) + cols - 1) // cols
            tile_w, tile_h = images[0].size
            canvas = Image.new("RGB", (cols * tile_w, rows * tile_h))
            for idx, img in enumerate(images):
                x = (idx % cols) * tile_w
                y = (idx // cols) * tile_h
                canvas.paste(img, (x, y))

        canvas.save(str(output_path))

        # Every input row maps to the same mosaic output (1-to-1).
        return [
            self.Outputs(mosaic_path=output_path, image_count=len(images))
            for _ in arguments_list
        ]
