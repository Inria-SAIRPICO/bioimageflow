"""Small NumPy filters used when optional restoration libraries are absent."""

from typing import Any


def gaussian_kernel1d(sigma: float) -> Any:
    import numpy as np

    sigma = max(float(sigma), 0.2)
    radius = max(1, int(3.0 * sigma + 0.5))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(x**2) / (2.0 * sigma**2))
    return kernel / kernel.sum()


def gaussian_blur(image: Any, sigma: float) -> Any:
    import numpy as np

    kernel = gaussian_kernel1d(sigma)
    result = image.astype(np.float32, copy=False)
    for axis in range(result.ndim):
        pad = [(0, 0)] * result.ndim
        pad[axis] = (len(kernel) // 2, len(kernel) // 2)
        padded = np.pad(result, pad, mode="edge")
        result = np.apply_along_axis(
            lambda line: np.convolve(line, kernel, mode="valid"), axis, padded
        )
    return result.astype(np.float32)
