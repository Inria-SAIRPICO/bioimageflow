# ConnectedComponents

`ConnectedComponents` converts a binary foreground image into a label image.
Every face-connected non-zero region receives a unique integer label; pixels or voxels touching only at a corner remain separate components.

The input is `input_image`, a planar or volumetric binary image.
Outputs are `output_image`, a UInt32 label image, and `num_labels`, the number of connected components.
The implementation reads and writes with imageio and labels with scikit-image.

Use it after thresholding, spot detection, or mask cleanup when instance labels are needed.
Empty foreground produces a label count of zero.
Missing files or unsupported formats fail through imageio.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, scikit-image, and NumPy.

## Assumptions

Non-zero pixels or voxels are foreground.
Connectivity is one orthogonal step in any dimension: 4-connected in 2D and 6-connected in 3D.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import ConnectedComponents

ConnectedComponents().process_row(Arguments(input_image="binary.tif"))
```

## Expected Results

The output label image has one integer label per connected foreground component.
It is written as `uint32`; background is `0`, labels are sequential positive component IDs, and `num_labels` matches the number of IDs.

## Failure Modes

Unsupported formats, missing files, and output write failures stop execution.
