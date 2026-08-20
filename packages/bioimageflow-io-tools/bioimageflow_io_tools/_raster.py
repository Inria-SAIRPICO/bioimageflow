"""Lightweight raster reading and writing helpers."""

from pathlib import Path
from typing import Any

from ._axes import normalize_axis_order, validate_nonnegative_index


def read_raster(input_image: Path | str) -> Any:
    import imageio.v3 as iio

    return iio.imread(input_image)


def read_scene(input_image: Path | str, scene: int) -> Any:
    import imageio.v3 as iio

    path = Path(input_image)
    validate_nonnegative_index("Scene", scene)
    if is_tiff_path(path):
        import tifffile

        with tifffile.TiffFile(path) as tif:
            scene_count = len(tif.series)
            if scene < scene_count:
                return tif.series[scene].asarray()
            raise IndexError(
                f"Scene index {scene} is out of range for {scene_count} TIFF series."
            )
    if scene == 0:
        return iio.imread(path)
    raise IndexError(f"Scene index {scene} is only available for multi-series TIFF files.")


def write_raster(
    data: Any,
    output_path: Path | str,
    *,
    axes: str | None = None,
) -> Path:
    import imageio.v3 as iio
    import numpy as np

    path = Path(output_path)
    if is_ome_tiff_path(path) or is_ome_zarr_path(path):
        raise ValueError(
            "ConvertImageFormat only writes ordinary image formats; use "
            "ConvertToOmeTiff or ConvertToOmeZarr for OME output."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.asarray(data)
    normalized_axes = (
        normalize_axis_order(axes, tuple(array.shape)) if axes is not None else None
    )
    kwargs: dict[str, Any] = {}
    if is_tiff_path(path):
        if normalized_axes is not None and normalized_axes.endswith("S"):
            kwargs["photometric"] = "rgb"
        elif is_grayscale_stack(array):
            kwargs["photometric"] = "minisblack"
    iio.imwrite(path, array, **kwargs)
    return path


def is_grayscale_stack(array: Any) -> bool:
    if array.ndim < 3:
        return False
    return not (array.ndim == 3 and array.shape[-1] in {3, 4})


def is_tiff_path(path: Path) -> bool:
    return path.suffix.lower() in {".tif", ".tiff"}


def is_ome_tiff_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".ome.tif") or name.endswith(".ome.tiff")


def is_ome_zarr_path(path: Path) -> bool:
    return path.name.lower().endswith(".ome.zarr")
