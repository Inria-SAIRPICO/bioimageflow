# LabelOverlaps

`LabelOverlaps` counts pixel co-occurrences between two 2D label images. It
produces one output row per `(reference_label, spot_label)` pair observed in
the union of non-zero pixels.

Inputs are `label_image` and `reference_image`. Outputs are `reference_label`,
`spot_label`, and `overlap_count`. Both images must have compatible shape and
label semantics.

Use it to connect detected spots or predicted objects to reference objects.
The tool does not solve matching by itself; it reports raw overlap counts that
can be filtered or summarized downstream.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, and NumPy.

## Assumptions

Both label images are aligned 2D arrays with compatible shape.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import LabelOverlaps

LabelOverlaps().process_row(
    Arguments(label_image="spots.tif", reference_image="nuclei.tif")
)
```

## Expected Results

The output table contains one row per observed label pair and its pixel overlap
count.

## Failure Modes

Mismatched shapes can fail during NumPy indexing; missing files fail through
imageio.
