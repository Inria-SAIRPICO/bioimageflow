# ConvertToOmeZarr

`ConvertToOmeZarr` writes a single-scale OME-Zarr v2 store with the maintained `bioio-ome-zarr` writer and then reopens it through BioIO to verify shape and dtype.

## Inputs

- `input_image`: image file to convert.
- `dimension_order`: optional axis order.
  Reader-provided axes are used when they are unambiguous; otherwise this input is required.

## Outputs

- `output_image`: OME-Zarr directory, defaulting to `{input_image.stem}.ome.zarr`.

## Dependencies and Core Libraries

BioIO, the pinned BioIO reader plugins, and `bioio-ome-zarr` run in the tool's dedicated environment.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ConvertToOmeZarr

ConvertToOmeZarr().process_row(
    Arguments(
        input_image="volume.tif",
        dimension_order="ZYX",
        output_image="volume.ome.zarr",
    )
)
```

## Expected Results

The writer creates valid NGFF metadata and image data that BioIO can reopen with the declared shape and dtype.

## Failure Modes

Ambiguous source axes without `dimension_order`, invalid axes, writer errors, and round-trip shape or dtype mismatches stop execution.
