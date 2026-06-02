# Cellpose 3 and StarDist Segmentation Workflow

## Analysis question

How do two common nuclei segmentation backends behave on the same microscopy
input channel, and which outputs should an analyst compare before choosing a
segmentation package?

## Data

The workflow is intended for public or local multi-channel TIFF microscopy
images. It lists images from `data_dir`, extracts the configured nuclei channel,
and sends the same channel image to Cellpose 3 and StarDist.

## Expected outputs

- A Cellpose mask image and cell count for each input image.
- A StarDist mask image and object count for each input image.
- Separate result columns so masks and counts can be inspected side by side.

## Test coverage

Default tests construct the graph and verify the package imports and node
wiring without running the heavy model backends. Execution should be covered by
an optional slow test with explicit model environments and public data.
