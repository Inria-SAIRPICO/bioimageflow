# InstanSeg Cell Segmentation

The workflow runs the official `fluorescence_nuclei_and_cells` model with `target="cells"`, writes a provenance-tracked mask, and calculates per-cell region areas.

It accepts a local microscopy image and explicit pixel size.
The public-data manifest points to the official InstanSeg examples and requires deployment-specific SHA-256 pinning of the selected image and resolved model.
