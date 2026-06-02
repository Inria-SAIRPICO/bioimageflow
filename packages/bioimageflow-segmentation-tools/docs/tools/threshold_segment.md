# ThresholdSegment

`ThresholdSegment` creates a foreground mask from an intensity or probability
image and labels each connected component.

Inputs are `input_image`, `threshold`, and `above`. Output `labels` is a label
image; `object_count` is the maximum assigned component label. The implementation
uses package-local connected-component logic over the generated foreground mask.

Use it for simple, demonstrative segmentation and as a deterministic baseline
for tests. If objects touch, they remain one component; use `WatershedSegment`
when marker-controlled splitting is needed.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy, and the package's connected-component
labeling helper.

## Assumptions

Foreground can be separated by one scalar threshold, and connected foreground
components represent objects.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import ThresholdSegment

ThresholdSegment().process_row(
    Arguments(input_image="image.tif", threshold=5.0, above=True)
)
```

## Expected Results

The label image contains sequential non-zero labels for connected foreground
objects and `object_count` equals the number of objects.

## Failure Modes

Missing images fail through imageio. Touching objects are not split. A poor
threshold can produce empty or over-merged labels.
