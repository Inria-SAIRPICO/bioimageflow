"""Mosaic — aggregate multiple images into a single grid montage."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
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
        ]
        columns: Annotated[int, GUIMeta(min=1, max=100, step=1)] = 5
        tile_size: int | None = None

    class Outputs(IOModel):
        mosaic_path: Path = Path("{node_name}_mosaic.png")
        image_count: int

    def process_batch(self, arguments_list: list[Arguments]) -> Any:
        from PIL import Image

        images = []
        for args in arguments_list:
            img = Image.open(str(args.input_image))
            if args.tile_size is not None:
                img = img.resize((args.tile_size, args.tile_size))
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
