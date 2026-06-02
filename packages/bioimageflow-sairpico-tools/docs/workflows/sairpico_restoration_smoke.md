# SAIRPICO Restoration Smoke Workflow

## Purpose

`example-workflows/sairpico_restoration_smoke/workflow.py` is the package demo
workflow for SAIRPICO command-wrapper integration. It builds a tiny synthetic
2D input image, runs `MedianDenoising`, feeds the denoised output into
`RichardsonLucyDeconvolution`, and collects both outputs.

The workflow is intentionally small. Tests monkeypatch the SAIRPICO subprocess
runner so command construction and BioImageFlow graph wiring can be validated
without requiring the external SAIRPICO binaries in the default test
environment.

## Analysis question

Can BioImageFlow orchestrate SAIRPICO restoration commands, preserve the
image-to-image dependency between denoising and deconvolution, and report the
produced artifacts?

## Inputs

- `storage_path`: workflow storage root. Default
  `./sairpico_restoration_smoke_results`.
- Synthetic input image written by the workflow to
  `<storage_path>/data/synthetic_sairpico_input.tif`.

## Data

Default tests use the generated synthetic input image above and monkeypatch the
external commands. Real runs use the analyst's local microscopy image data and
the SAIRPICO binaries documented by each command wrapper.

## Workflow Steps

- `median_denoise_2d`: calls `MedianDenoising` with `denoising_type="2D"`,
  `radius_x=1`, `radius_y=1`, and `padding=True`.
- `richardson_lucy_2d`: calls `RichardsonLucyDeconvolution` on the denoised
  image with `deconvolution_type="2D"`, `sigma=1.2`, `niter=3`,
  `regularization_lambda=0.01`, and `padding=True`.
- `collect_sairpico_outputs`: collects the denoising and deconvolution outputs.

## Outputs

- Denoised image from `MedianDenoising`.
- Deconvolved image from `RichardsonLucyDeconvolution`.
- A terminal result row containing both collected output paths.

## Assumptions

- The workflow uses `use_wetlands=False`.
- Real execution requires the SAIRPICO `simgmedian2d` and
  `simgrichardsonlucy2d` commands.
- Default tests validate command construction with fake subprocess behavior,
  not restoration quality.

## Dependencies and Core Libraries

- `bioimageflow.Workflow`.
- `bioimageflow_common_tools.Collect`.
- `bioimageflow_sairpico_tools.MedianDenoising`.
- `bioimageflow_sairpico_tools.RichardsonLucyDeconvolution`.
- `imageio.v3` and `numpy` for synthetic fixture generation.

## Minimal Example

```python
from pathlib import Path
import importlib.util

workflow_path = Path("example-workflows/sairpico_restoration_smoke/workflow.py")
spec = importlib.util.spec_from_file_location("sairpico_restoration_smoke", workflow_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

workflow, terminal = module.build_workflow("./sairpico_results")
result = workflow.compute(terminal)
print(result)
```

## Expected Results

With real binaries or a test monkeypatch that writes outputs, the workflow
returns one result row. Both collected output image paths exist, and the command
sequence is `simgmedian2d` followed by `simgrichardsonlucy2d`.

## Test coverage

The priority workflow test executes the graph with monkeypatched command
execution, asserts the command sequence and dependency wiring, and verifies both
output files exist. SAIRPICO environment/version checks are test diagnostics,
not public workflow tools.

## Failure Modes

- SAIRPICO commands are missing during real execution.
- The synthetic input or workflow storage directories cannot be written.
- Deconvolution fails if the upstream denoised image is missing.
- Subprocess calls exit non-zero because the runtime environment is incomplete.
