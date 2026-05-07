"""Spot detection with LoG/DoG filtering and local maxima extraction."""

from pathlib import Path
from typing import Annotated, Any
import csv

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)


def _gaussian_kernel1d(sigma: float) -> "Any":
    import numpy as np

    sigma = max(float(sigma), 0.2)
    radius = max(1, int(3.0 * sigma + 0.5))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(x**2) / (2.0 * sigma**2))
    return kernel / kernel.sum()


def _blur(image: "Any", sigma: float) -> "Any":
    import numpy as np

    kernel = _gaussian_kernel1d(sigma)
    result = image.astype(np.float32, copy=False)
    for axis in range(result.ndim):
        pad = [(0, 0)] * result.ndim
        pad[axis] = (len(kernel) // 2, len(kernel) // 2)
        padded = np.pad(result, pad, mode="edge")
        result = np.apply_along_axis(
            lambda line: np.convolve(line, kernel, mode="valid"), axis, padded
        )
    return result


def _laplace(image: "Any") -> "Any":
    import numpy as np

    if image.ndim != 2:
        raise ValueError("DetectSpots currently supports 2D scalar images.")
    padded = np.pad(image, 1, mode="edge")
    center = padded[1:-1, 1:-1]
    return (
        padded[:-2, 1:-1]
        + padded[2:, 1:-1]
        + padded[1:-1, :-2]
        + padded[1:-1, 2:]
        - 4.0 * center
    )


def _log_score(image: "Any", sigma: float) -> "Any":
    import numpy as np

    try:
        from scipy.ndimage import gaussian_laplace
    except ImportError:
        return (-(sigma**2) * _laplace(_blur(image, sigma))).astype(np.float32)
    return (-(sigma**2) * gaussian_laplace(image, sigma=sigma)).astype(np.float32)


def _score_image(image: "Any", method: str, sigma: float, sigma_ratio: float) -> "Any":
    method = method.lower()
    if method not in {"dog", "log", "local_maxima"}:
        raise ValueError("method must be 'dog', 'log', or 'local_maxima'")
    if method == "local_maxima":
        return image.astype("float32", copy=False)
    if method == "log":
        return _log_score(image, sigma)
    narrow = _blur(image, sigma)
    wide = _blur(image, sigma * sigma_ratio)
    return narrow - wide


def _local_maxima(score: "Any", threshold: float, min_distance: int) -> list[tuple[int, int]]:
    import numpy as np

    if score.ndim != 2:
        raise ValueError("DetectSpots currently supports 2D scalar images.")
    radius = max(1, int(min_distance))
    candidates = np.argwhere(score > float(threshold))
    candidates = sorted(candidates, key=lambda yx: float(score[tuple(yx)]), reverse=True)
    accepted: list[tuple[int, int]] = []
    occupied = np.zeros(score.shape, dtype=bool)
    for y, x in candidates:
        y0 = max(0, int(y) - radius)
        y1 = min(score.shape[0], int(y) + radius + 1)
        x0 = max(0, int(x) - radius)
        x1 = min(score.shape[1], int(x) + radius + 1)
        window = score[y0:y1, x0:x1]
        if float(score[y, x]) < float(window.max()):
            continue
        if occupied[y0:y1, x0:x1].any():
            continue
        accepted.append((int(y), int(x)))
        occupied[y0:y1, x0:x1] = True
    return sorted(accepted)


class DetectSpots(ProcessingTool):
    """Detect puncta as local maxima in LoG/DoG filtered images."""

    display_name = "Detect Spots"
    documentation = (
        "Detect puncta with Difference-of-Gaussians, Laplacian-of-Gaussian, "
        "or direct local maxima. Big-FISH is intentionally optional and not "
        "required for default execution."
    )
    category = Category.SPOT_DETECTION
    tags = ["spots", "puncta", "dog", "log"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Input image",
                description="2D intensity image containing puncta.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        method: str = "dog"
        sigma: Annotated[float, GUIMeta(min=0.2, max=10.0, step=0.1)] = 1.0
        sigma_ratio: Annotated[float, GUIMeta(min=1.1, max=4.0, step=0.1)] = 1.6
        threshold: float = 0.1
        min_distance: int = 3

    class Outputs(IOModel):
        output_labels: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Spot labels"),
        ] = Template("{input_image.stem}_spots.tif")
        spots_csv: Annotated[Path, GUIMeta(display_name="Spot table")] = Template(
            "{input_image.stem}_spots.csv"
        )
        spot_count: Annotated[int, GUIMeta(display_name="Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.input_image).astype(np.float32)
        score = _score_image(
            image,
            method=str(arguments.method),
            sigma=float(arguments.sigma),
            sigma_ratio=float(arguments.sigma_ratio),
        )
        maxima = _local_maxima(
            score,
            threshold=float(arguments.threshold),
            min_distance=int(arguments.min_distance),
        )

        labels = np.zeros(image.shape, dtype=np.uint16)
        rows = []
        for spot_id, (y, x) in enumerate(maxima, start=1):
            labels[y, x] = spot_id
            rows.append(
                {
                    "spot_id": spot_id,
                    "y": y,
                    "x": x,
                    "intensity": float(image[y, x]),
                    "score": float(score[y, x]),
                }
            )

        output_labels = Path(arguments.output_labels)
        spots_csv = Path(arguments.spots_csv)
        output_labels.parent.mkdir(parents=True, exist_ok=True)
        spots_csv.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output_labels, labels)
        with spots_csv.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["spot_id", "y", "x", "intensity", "score"]
            )
            writer.writeheader()
            writer.writerows(rows)
        return self.Outputs(
            output_labels=output_labels,
            spots_csv=spots_csv,
            spot_count=len(rows),
        )
