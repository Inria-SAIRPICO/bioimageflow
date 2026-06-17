# ConnectedComponents

`ConnectedComponents` converts a binary foreground image into a label image.
Every connected non-zero region receives a unique integer label.

The input is `input_image`, a planar or volumetric binary image. Outputs are
`output_image`, a UInt32 label image, and `num_labels`, the number of connected
components. The core libraries are SimpleITK for input IO and connected
component labeling, plus tifffile for UInt32 TIFF output.

Use it after thresholding, spot detection, or mask cleanup when instance labels
are needed. Empty foreground produces a label count of zero. Missing files or
unsupported formats fail through SimpleITK.

## Dependencies and Core Libraries

BioImageFlow core APIs, SimpleITK, tifffile, and NumPy for counting labels.

## Assumptions

Non-zero pixels or voxels are foreground, and SimpleITK connectivity semantics
are acceptable for the workflow.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import ConnectedComponents

ConnectedComponents().process_row(Arguments(input_image="binary.tif"))
```

## Expected Results

The output label image has one integer label per connected foreground component.
It is written as `uint32`; background is `0`, and labels are positive component IDs.

## Failure Modes

Unsupported formats, missing files, and output write failures stop execution.
