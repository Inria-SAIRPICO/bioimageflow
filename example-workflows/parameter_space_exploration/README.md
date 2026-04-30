# Parameter Space Exploration Workflow

This example demonstrates how to use BioImageFlow to systematically explore algorithm parameter spaces using a combinatorial DAG pattern.

## Overview

The workflow:

1. Lists input images using the `Files` tool.
2. Generates parameter value lists using the `Generate` tool (one for sensitivity, one for size).
3. Performs a Cartesian product using `CrossJoin`.
4. Executes `Atlas` spot detection on each image/parameter combination.
5. Creates a mosaic of all detection results using the `Mosaic` tool, which accepts scalar image semantics including Atlas's binary masks.

## Prerequisites

- BioImageFlow installed (both `bioimageflow-core` and `bioimageflow`)
- `bioimageflow-common-tools` containing the tools used here.
- The Atlas CLI tool must be available in the environment (installed via conda-forge: `conda install -c bioimageit atlas`).
- Pillow and numpy for the Mosaic tool (installed in the main Python environment).

## Running the Workflow

```bash
# From the repository root
python -m example-workflows.parameter_space_exploration.workflow /path/to/data /path/to/output
```

Or run directly:

```bash
python example-workflows/parameter_space_exploration/workflow.py ./data ./results
```

## Expected Output

- A mosaic image (`detections_mosaic.png`) containing all spot detection results arranged in a grid.
- Console output showing the mosaic path and total image count.

## Customization

You can modify the parameter values, image pattern, mosaic columns, and tile size by editing `workflow.py`.

## GUI editing

The dynamic-output schema added by `Generate.resolve_outputs` and the merge-tool `resolve_merge_schema` overrides means that this workflow is also buildable in the BioImageFlow platform GUI: per-column output pins for `Generate` and the downstream `CrossJoin` are resolved as soon as `column_name` is set, so dragging `param_grid["sensitivity"]` into `Atlas.p_value` works without any Python.
