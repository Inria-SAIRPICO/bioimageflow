# ConvertToOmeTiff

`ConvertToOmeTiff` reads an image file and converts it to OME-TIFF using
tifffile. It is a focused converter for the common workflow step where an
intermediate TIFF or selected analysis plane must be normalized for viewers or
downstream tools that expect OME metadata.

## Inputs

- `input_image`: image file to convert.
- `dimension_order`: optional OME axis order. When omitted, the tool infers
  `YX`, `ZYX`, `CZYX`, or `TCZYX` from dimensionality.

## Outputs

- `output_image`: converted OME-TIFF image, defaulting to
  `{input_image.stem}.ome.tiff`.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio for reading, tifffile for OME-TIFF writing, and
NumPy through the image stack.

## Assumptions

The input image has already been selected or arranged into the axis order
declared by `dimension_order`. This converter writes a single OME-TIFF image
and does not perform pyramid generation, intensity normalization, or
Bio-Formats metadata translation.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ConvertToOmeTiff

ConvertToOmeTiff().process_row(
    Arguments(
        input_image="selected_plane.tif",
        output_image="selected_plane.ome.tiff",
        dimension_order="YX",
    )
)
```

## Expected Results

The OME-TIFF exists, preserves the input pixel values, and tifffile reports the
requested series axes.

## Failure Modes

Unreadable inputs, invalid axis metadata, unsupported dimensionality without an
explicit `dimension_order`, and filesystem write failures stop execution.
