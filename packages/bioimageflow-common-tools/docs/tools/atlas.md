# Atlas

`Atlas` is a legacy module-level wrapper around the external Atlas spot
detection CLI. It detects sparse spots in 2D intensity TIFF images and writes a
binary detection mask.

The tool is currently importable from `bioimageflow_common_tools.atlas`, but it
is not re-exported from `bioimageflow_common_tools.__init__`. Existing FISH and
parameter-exploration examples still use it. Future work should move or
re-export this behavior from `bioimageflow-spot-tools`.

Inputs are `input_image`, optional `gaussian_std`, optional `p_value`, optional
`area_lim`, and `verbose`. Output is `output_image`, a binary TIFF detection
mask. The tool requires an `ExecutionContext` with `row_dir` because the Atlas
CLI writes implicit files in the process working directory.

Core dependencies are BioImageFlow core APIs and the external `bioimageit::atlas`
conda package. The wrapper also manages a shared `blobs.txt` Atlas reference in
the workflow work directory when packaged reference data is unavailable.

```python
from bioimageflow_common_tools.atlas import Atlas

spots = Atlas()(
    input_image=image["output_image"],
    p_value=0.05,
    gaussian_std=2,
    name="atlas_spots",
)
```

Expected result: `output_image` points to a binary mask where non-zero pixels
represent detected spots. Failure modes include missing Atlas or `blobsref`
binaries, missing `context.row_dir`, unsupported input format, inability to
write the shared reference, or non-zero CLI exit status.

## Dependencies and Core Libraries

BioImageFlow core APIs, the external Atlas CLI, and the `bioimageit::atlas`
conda package.

## Assumptions

The input is a 2D TIFF intensity image and execution happens inside a
BioImageFlow row context with a writable row work directory.

## Minimal Example

The example above constructs an Atlas node in a workflow graph.

## Expected Results

The wrapper writes a binary detection mask at `output_image`.

## Failure Modes

Missing binaries, missing row context, unsupported inputs, or non-zero CLI exit
status stop execution.
