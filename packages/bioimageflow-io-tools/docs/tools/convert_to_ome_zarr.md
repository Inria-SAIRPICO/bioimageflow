# ConvertToOmeZarr

`ConvertToOmeZarr` reads an image file and converts it to a single-scale,
uncompressed OME-Zarr v2 directory. It is intentionally lightweight and useful
for smoke-tested workflow demos, simple local handoffs, and validating that
downstream tools can consume an NGFF-like directory.

For production multiscale pyramids, chunk tuning, compression, or very large
datasets, use a future BioIO or ome-zarr-backed converter instead.

## Inputs

- `input_image`: image file to convert.

## Outputs

- `output_image`: converted OME-Zarr directory, defaulting to
  `{input_image.stem}.ome.zarr`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio for reading, NumPy, and the package's minimal
OME-Zarr v2 writer.

## Assumptions

The input image is small enough for the lightweight single-scale writer and has
already been sliced to the desired array. This converter creates a minimal
OME-Zarr v2 directory for deterministic workflow exchange, not a production
multiscale NGFF export.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ConvertToOmeZarr

ConvertToOmeZarr().process_row(
    Arguments(input_image="selected_plane.tif", output_image="selected_plane.ome.zarr")
)
```

## Expected Results

The output directory contains `.zgroup`, `.zattrs`, a `0/.zarray` metadata file,
and the first array chunk. Metadata includes a single multiscales entry.

## Failure Modes

Unreadable inputs and filesystem write failures stop execution. The converter
does not build pyramids, compression, or chunk layouts optimized for large
production data.
