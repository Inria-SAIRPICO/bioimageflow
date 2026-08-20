# AtlasSpotDetection

`AtlasSpotDetection` wraps the external Atlas spot detection CLI.
It detects sparse spots in 2D intensity TIFF images and writes a binary detection mask.

Use this tool when a workflow specifically needs the Atlas CLI behavior.
For lightweight deterministic puncta detection without the external Atlas binary, use `DetectSpots`.

Inputs are `input_image`, optional `gaussian_std`, optional `p_value`, optional `area_lim`, and `verbose`.
Output is `output_image`, a binary TIFF detection mask.
Inside a workflow, the tool uses the row directory from its `ExecutionContext` to isolate implicit Atlas CLI files.
Direct `process_row` calls without a context receive an isolated temporary work and row directory automatically.

Core dependencies are BioImageFlow core APIs and the external `bioimageit::atlas` conda package.
The wrapper also uses a packaged `blobs.txt` Atlas reference, with a generated shared fallback in the workflow work directory when packaged reference data is unavailable.

```python
from bioimageflow_spot_tools import AtlasSpotDetection

spots = AtlasSpotDetection()(
    input_image=image["output_image"],
    p_value=0.05,
    gaussian_std=2,
    name="atlas_spots",
)
```

Expected result: `output_image` points to a binary mask where non-zero pixels represent detected spots.

## Dependencies and Core Libraries

BioImageFlow core APIs, the external Atlas CLI, and the `bioimageit::atlas` conda package.

## Assumptions

The input is a 2D TIFF intensity image.
Workflow execution requires a writable row directory; direct calls use a temporary directory that is removed after execution.

## Failure Modes

Missing Atlas or `blobsref` binaries, a workflow context without `row_dir`, unsupported inputs, inability to write the shared reference, or non-zero CLI exit status stop execution.
