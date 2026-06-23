# Parameter Space Exploration Workflow

This example demonstrates how to use BioImageFlow to systematically explore algorithm parameter spaces using a combinatorial DAG pattern.

## Goal

Which Atlas sensitivity and spot-size parameters produce useful spot-detection
results for a microscopy image collection, and how can the outputs be inspected
as a grid?

## Overview

The workflow:

1. Lists input images using the `Files` tool.
2. Generates parameter value lists using the `Generate` tool (one for sensitivity, one for size).
3. Performs a Cartesian product using `CrossJoin`.
4. Executes `AtlasSpotDetection` on each image/parameter combination.
5. Counts connected foreground spots and foreground fraction for each ATLAS mask.
6. Creates a mosaic of all detection results using the `Mosaic` tool, which accepts scalar image semantics including Atlas's binary masks.

## Prerequisites

- BioImageFlow installed (both `bioimageflow-core` and `bioimageflow`)
- `bioimageflow-common-tools` containing the tools used here.
- The Atlas CLI tool must be available in the environment (installed via conda-forge: `conda install -c bioimageit atlas`).
- Pillow and numpy for the Mosaic tool (installed in the main Python environment).

## Running the Workflow

```bash
# From the repository root
python example_workflows/parameter_space_exploration/workflow.py ./data ./results
```

## Results

- A parameter-results table with one row per image and parameter combination.
- Spot-count and foreground-fraction measurements for each ATLAS mask.
- A mosaic image containing all spot detection results arranged in a grid.
- Console output showing the mosaic path and total parameter rows.

## Data

Use a local or public directory of fluorescence microscopy TIFF images. The
workflow lists files from `data_dir` with the configured glob pattern. Default
tests construct the graph without running Atlas.

## Validation

Use a known FISH marker crop and the Atlas binary to compare masks and counts across the parameter grid.

## Customization

You can modify the parameter values, image pattern, mosaic columns, and tile size by editing `workflow.py`.

## GUI editing

The dynamic-output schema added by `Generate.resolve_outputs` and the merge-tool `resolve_merge_schema` overrides means that this workflow is also buildable in the BioImageFlow platform GUI: per-column output pins for `Generate` and the downstream `CrossJoin` are resolved as soon as `column_name` is set, so dragging `param_grid["sensitivity"]` into `AtlasSpotDetection.p_value` works without any Python.
