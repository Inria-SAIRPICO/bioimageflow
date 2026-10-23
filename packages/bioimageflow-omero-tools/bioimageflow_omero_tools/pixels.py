"""Lossless basic TIFF/PNG and complete TCZYX plane conversion."""

import logging
from pathlib import Path
from typing import Any, Iterator

from .connection import OmeroToolError


PIXEL_DTYPES = frozenset({"int8", "uint8", "int16", "uint16", "int32", "uint32", "float32", "float64"})
AXES = "TCZYX"


def read_upload_image(path: Path) -> Any:
    """Validate and decode the entire local input before opening a session."""
    import numpy as np

    if not path.is_file():
        raise OmeroToolError("Upload image must be an existing TIFF, OME-TIFF or PNG file.")
    try:
        if path.suffix.lower() in {".tif", ".tiff"}:
            array = _read_tiff(path)
        elif path.suffix.lower() == ".png":
            array = _read_png(path)
        else:
            raise OmeroToolError("Upload supports TIFF, OME-TIFF and PNG files only.")
    except OmeroToolError:
        raise
    except Exception:
        raise OmeroToolError("Upload image could not be decoded losslessly.") from None
    # The gateway assumes native byte order before performing its wire conversion.
    return array.astype(np.dtype(array.dtype).newbyteorder("="), copy=False)


def _read_tiff(path: Path) -> Any:
    """Read one explicitly labelled scalar series; never guess a Q/S axis."""
    import numpy as np
    import tifffile

    with tifffile.TiffFile(path) as image:
        if len(image.series) != 1:
            raise OmeroToolError("Upload requires exactly one TIFF image series.")
        series = image.series[0]
        axes = series.axes
        if not axes.endswith("YX") or len(set(axes)) != len(axes) or set(axes) - set(AXES):
            raise OmeroToolError("TIFF axes must unambiguously describe scalar T, C, Z, Y and X dimensions.")
        if np.dtype(series.dtype).name not in PIXEL_DTYPES:
            raise OmeroToolError("TIFF pixel dtype is not losslessly supported by OMERO.")
        if any(size < 1 for size in series.shape):
            raise OmeroToolError("TIFF dimensions must be non-empty.")
        array = series.asarray()
    order = [axes.index(axis) for axis in AXES if axis in axes]
    array = np.transpose(array, order)
    sizes = dict(zip((axis for axis in AXES if axis in axes), array.shape))
    shape = tuple(sizes.get(axis, 1) for axis in AXES)
    return array.reshape(shape)


def _read_png(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise OmeroToolError("Upload requires a single PNG image, not an animation.")
        if image.mode not in {"L", "I;16", "I;16B", "RGB", "RGBA"}:
            raise OmeroToolError("PNG must be grayscale uint8/uint16 or RGB/RGBA uint8; palettes are unsupported.")
        if "transparency" in image.info:
            raise OmeroToolError("PNG transparency keys are unsupported; use explicit RGBA pixels.")
        array = np.asarray(image)
        if image.mode in {"RGB", "RGBA"}:
            return np.transpose(array, (2, 0, 1))[None, :, None, :, :]
        return array[None, None, None, :, :]


def upload_planes(array: Any) -> Iterator[Any]:
    """The public OMERO gateway consumes planes with T varying fastest."""
    size_t, size_c, size_z, _size_y, _size_x = array.shape
    for z in range(size_z):
        for c in range(size_c):
            for t in range(size_t):
                yield array[t, c, z]


def download_image(image: Any, destination: Path) -> tuple[tuple[int, ...], str]:
    import numpy as np
    import tifffile

    shape = tuple(int(size) for size in (
        image.getSizeT(), image.getSizeC(), image.getSizeZ(), image.getSizeY(), image.getSizeX(),
    ))
    if any(size < 1 for size in shape):
        raise OmeroToolError("OMERO image dimensions must be non-empty.")
    pixels = image.getPrimaryPixels()
    dtype = np.dtype(pixels.get_numpy_type())
    if dtype.name not in PIXEL_DTYPES:
        raise OmeroToolError("OMERO pixel dtype cannot be preserved in the supported TIFF transfer.")
    size_t, size_c, size_z, size_y, size_x = shape
    coordinates = [(z, c, t) for t in range(size_t) for c in range(size_c) for z in range(size_z)]

    def planes() -> Iterator[Any]:
        count = 0
        for plane in pixels.getPlanes(coordinates):
            if tuple(plane.shape) != (size_y, size_x) or np.dtype(plane.dtype).name != dtype.name:
                raise OmeroToolError("OMERO returned inconsistent pixel planes.")
            count += 1
            if count > len(coordinates):
                raise OmeroToolError("OMERO returned too many pixel planes.")
            yield plane
        if count != len(coordinates):
            raise OmeroToolError("OMERO returned incomplete pixel planes.")

    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            tifffile.imwrite(
                stream, data=planes(), shape=shape, dtype=dtype, ome=True,
                photometric="minisblack", metadata={"axes": AXES},
            )
    except BaseException:
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("Incomplete owned OMERO download could not be removed.")
        raise
    return shape, dtype.name
