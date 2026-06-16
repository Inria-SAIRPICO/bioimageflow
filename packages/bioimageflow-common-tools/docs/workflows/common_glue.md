# Common Glue Workflow

This demo pattern combines `Files`, `Generate`, one of the join tools, and an
analysis node from another package. It is valuable because many BioImageFlow
workflows start by creating a table of image paths, expanding it with parameter
values, and then joining downstream results.

## Analysis Question

How can an analyst turn a folder of microscopy images and a small parameter
set into a reproducible workflow table that downstream analysis tools can
consume without losing row provenance?

## Data

Use a tiny directory containing one or more TIFF images. Tests should create the
images with NumPy and imageio so the example is deterministic and does not
depend on external downloads.

## Expected Results

The source table contains a `path` column. Parameter expansion with `CrossJoin`
adds one row per image-parameter combination. Downstream table operations must
preserve row lineage and produce deterministic column names for overlapping
inputs.

## Minimal Example

```python
from bioimageflow import Workflow
from bioimageflow_common_tools import CrossJoin, Files, Generate

with Workflow(storage_path="results") as wf:
    images = Files()(path="demo-images", pattern="*.tif", name="images")
    thresholds = Generate()(
        column_name="threshold",
        values=[0.2, 0.5, 0.8],
        name="thresholds",
    )
    expanded = CrossJoin()(images, thresholds, name="expanded")
```
