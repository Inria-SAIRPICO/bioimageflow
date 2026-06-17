# bioimageflow-segmentation-tools

`bioimageflow-segmentation-tools` contains segmentation wrappers for classical
threshold/watershed workflows and optional deep-learning models. It is the
package to install when a workflow needs label images from intensity,
probability, or binary inputs.

Install-time libraries are imageio, NumPy, scikit-image, and tifffile.
Cellpose, StarDist, TensorFlow, and other model-runtime dependencies live in isolated `EnvironmentSpec` environments and are imported inside `process_row`, so graph construction and schema tests remain lightweight.

## Tools

- [ThresholdSegment](#thresholdsegment): threshold and connected
  component labeling.
- [OtsuThresholdSegment](#otsuthresholdsegment): global Otsu thresholding
  with connected-component labeling.
- [LocalThresholdSegment](#localthresholdsegment): Sauvola adaptive
  thresholding with connected-component labeling.
- [WatershedSegment](#watershedsegment): marker-controlled foreground
  splitting.
- [DistanceWatershedSegment](#distancewatershedsegment): marker-free
  distance-transform watershed.
- [SplitTouchingObjects](#splittouchingobjects): split clumped label masks
  with distance watershed semantics.
- [FilterLabels](#filterlabels): filter labels by area, border contact,
  intensity, and shape.
- [PostprocessLabels](#postprocesslabels): minimum-size filtering and
  sequential relabeling.
- [Cellpose3](#cellpose3): Cellpose v3 pretrained model wrapper.
- [StarDistSegmenter](#stardistsegmenter): StarDist 2D pretrained
  model wrapper.

## Demo Workflow

- [BBBC038 segmentation benchmark](#bbbc038-segmentation-benchmark-workflow):
  synthetic nuclei segmentation with a path to public BBBC038 validation.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-segmentation-tools/tests
```

Fast tests execute only the classical tools on generated images and verify that heavy tools build workflow graphs without importing model dependencies.
Optional model-runtime validation should be marked `complete`, `wetlands`, and `model_runtime`; public datasets additionally use `public_data` and define expected object-count or benchmark ranges.
