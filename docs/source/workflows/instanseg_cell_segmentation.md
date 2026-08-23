# InstanSeg Cell Segmentation

The `instanseg_cell_segmentation` workflow applies InstanSeg's official `fluorescence_nuclei_and_cells` model to a YX or CYX fluorescence image and measures the resulting cell instances.
It is intended for routine fluorescence cell segmentation where physical pixel size and channel selection must remain explicit.

Run it from the repository root:

```bash
python example_workflows/instanseg_cell_segmentation/workflow.py --input-image data/image.tif --pixel-size-um 0.5
```

The workflow writes a uint32 instance-label image, records model and inference provenance, and returns one area measurement per cell.
The data manifest points to the official InstanSeg examples; deployments must pin the selected image and resolved model bundle by SHA-256 before enabling the complete public-data test.
