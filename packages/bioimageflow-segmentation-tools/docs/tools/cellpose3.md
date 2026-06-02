# Cellpose3

`Cellpose3` wraps Cellpose v3 pretrained models such as `cyto3` and `nuclei`.
It segments 2D intensity or channel images and writes a label mask.

Inputs include `input_image`, `diameter`, `model_type`, Cellpose channel
selectors, `flow_threshold`, and `cellprob_threshold`. Outputs are `mask` and
`cell_count`. The core dependency is `cellpose==3.1.1.1`, isolated in its own
environment spec.

Use it for cell or nucleus segmentation when a pretrained Cellpose v3 model is
appropriate and model dependencies are available. The package avoids importing
Cellpose during schema and graph construction; runtime failures occur if the
environment cannot install or import Cellpose, if the model name is invalid, or
if the input image layout does not match the channel settings.

## Dependencies and Core Libraries

BioImageFlow core APIs, Cellpose 3.1.1.1, imageio, NumPy, packaging, and
tifffile in the isolated Cellpose environment.

## Assumptions

The input is a 2D intensity or channel image compatible with the selected
Cellpose model and channel selectors.

## Minimal Example

```python
from bioimageflow_segmentation_tools import Cellpose3

cellpose = Cellpose3()(
    input_image="cells.tif",
    model_type="cyto3",
    diameter=25.0,
)
```

## Expected Results

At runtime with Cellpose installed, `mask` is a label image and `cell_count`
equals the maximum non-background label.

## Failure Modes

Missing Cellpose dependencies, invalid model names, incompatible channels, GPU
or memory limits, and unreadable inputs can fail execution.
