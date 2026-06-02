# OME Normalization Workflow

This workflow demonstrates a common ingestion pattern: read a local microscopy
image, select the analysis plane/channel, and convert the selected image to
OME-compatible outputs for downstream tools.

## Analysis question

Can a workflow normalize a microscopy image into exchange formats while keeping
the intended channel/Z selection and pixel values unchanged?

## Data

Use a generated `CZYX`, `TCYX`, or `TZYX` TIFF with known shape. Tests should
write the array with imageio and assert exact output pixels after selection.

## Expected Results

- `ReadImage` produces a workflow-local copy with unchanged pixel values.
- `SelectDimensions` removes only the requested axes.
- `ConvertToOmeTiff` records the requested axis order in the TIFF series
  metadata.
- `ConvertToOmeZarr` creates `.zgroup`, `.zattrs`, and the first array chunk.

## Test coverage

The priority workflow test executes the generated CZYX fixture, checks the
selected YX plane against the expected NumPy slice, and verifies that both
converted artifacts exist.

```python
from bioimageflow import Workflow
from bioimageflow_io_tools import ConvertToOmeTiff, ReadImage, SelectDimensions

with Workflow(storage_path="results", use_wetlands=False) as wf:
    image = ReadImage()(input_image="source.tif", name="read")
    plane = SelectDimensions()(
        input_image=image["output_image"],
        layout="CZYX",
        channel=0,
        z=3,
        name="plane",
    )
    ome = ConvertToOmeTiff()(input_image=plane["output_image"], dimension_order="YX")
    wf.compute(ome)
```
