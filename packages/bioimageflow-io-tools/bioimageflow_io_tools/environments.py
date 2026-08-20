"""Execution environments for plugin-backed I/O tools."""

from bioimageflow_core import EnvironmentSpec


bioio_env = EnvironmentSpec(
    name="bioio-all",
    dependencies={
        "python": "3.12",
        "pip": [
            "bioio==3.4.0",
            "bioio-ome-zarr==3.5.1",
            "bioio-ome-tiff==1.4.0",
            "bioio-czi==2.8.0",
            "bioio-imageio==1.3.0",
            "bioio-tifffile==1.3.0",
            "bioio-tiff-glob==1.2.0",
            "imageio==2.37.3",
            "numpy==2.2.6",
            "tifffile==2025.5.10",
        ],
    },
)
