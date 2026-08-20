"""Spot detection with LoG/DoG filtering and local maxima extraction."""

from pathlib import Path
from typing import Annotated, Any, cast

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
    RowConsumption,
    Semantic,
    Template,
)

from .validation import finite_float, integral_value, planar_array


def _score_image(image: "Any", method: str, sigma: float, sigma_ratio: float) -> "Any":
    from scipy.ndimage import gaussian_filter, gaussian_laplace

    method = method.lower()
    if method not in {"dog", "log", "local_maxima"}:
        raise ValueError("method must be 'dog', 'log', or 'local_maxima'")
    if method == "local_maxima":
        return image.astype("float32", copy=False)
    if method == "log":
        return (
            -(sigma**2) * gaussian_laplace(image, sigma=sigma, mode="nearest")
        ).astype("float32")
    narrow = gaussian_filter(image, sigma=sigma, mode="nearest")
    wide = gaussian_filter(image, sigma=sigma * sigma_ratio, mode="nearest")
    return narrow - wide


def _local_maxima(
    score: "Any", threshold: float, min_distance: int
) -> list[tuple[int, int]]:
    import numpy as np
    from skimage.measure import label
    from skimage.morphology import local_maxima

    planar_array(score, "score image")
    maxima_mask = local_maxima(score, connectivity=2, allow_borders=True)
    maxima_mask &= score > threshold
    plateau_labels, plateau_count = cast(
        tuple[Any, int],
        label(
            maxima_mask,
            connectivity=2,
            return_num=True,
        ),
    )
    foreground_indices = np.flatnonzero(maxima_mask)
    plateau_ids = plateau_labels.ravel()[foreground_indices]
    first_indices = np.full(plateau_count + 1, score.size, dtype=np.intp)
    np.minimum.at(first_indices, plateau_ids, foreground_indices)
    candidates = [
        tuple(int(value) for value in np.unravel_index(flat_index, score.shape))
        for flat_index in first_indices[1:]
    ]
    candidates.sort(key=lambda yx: (-float(score[yx]), yx[0], yx[1]))

    accepted: list[tuple[int, int]] = []
    bucket_size = min_distance + 1
    occupied_buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for y, x in candidates:
        bucket_y, bucket_x = y // bucket_size, x // bucket_size
        nearby = (
            accepted_coordinate
            for nearby_bucket_y in range(bucket_y - 1, bucket_y + 2)
            for nearby_bucket_x in range(bucket_x - 1, bucket_x + 2)
            for accepted_coordinate in occupied_buckets.get(
                (nearby_bucket_y, nearby_bucket_x), []
            )
        )
        if any(
            max(abs(y - accepted_y), abs(x - accepted_x)) <= min_distance
            for accepted_y, accepted_x in nearby
        ):
            continue
        accepted.append((y, x))
        occupied_buckets.setdefault((bucket_y, bucket_x), []).append((y, x))
    return sorted(accepted)


class DetectSpots(ProcessingTool):
    """Detect puncta as local maxima in LoG/DoG filtered images."""

    row_consumption = RowConsumption.MAPPED
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
            ImageSpec(
                semantics={Semantic.LABEL}, layouts={Layout.PLANAR}, dtypes={"uint32"}
            ),
            GUIMeta(display_name="Spot labels"),
        ] = Template("{input_image.stem}_spots.tif")
        spot_id: Annotated[int, GUIMeta(display_name="Spot ID")]
        y: Annotated[float, GUIMeta(display_name="Y")]
        x: Annotated[float, GUIMeta(display_name="X")]
        intensity: Annotated[float, GUIMeta(display_name="Intensity")]
        score: Annotated[float, GUIMeta(display_name="Score")]
        spot_count: Annotated[int, GUIMeta(display_name="Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = planar_array(
            iio.imread(arguments.input_image).astype(np.float32),
            "input_image",
        )
        if not np.all(np.isfinite(image)):
            raise ValueError("input_image must contain only finite intensities.")
        method = str(arguments.method).lower()
        sigma = finite_float(arguments.sigma, "sigma")
        sigma_ratio = finite_float(arguments.sigma_ratio, "sigma_ratio")
        threshold = finite_float(arguments.threshold, "threshold")
        min_distance = integral_value(
            arguments.min_distance,
            "min_distance",
            minimum=1,
        )
        if sigma <= 0:
            raise ValueError("sigma must be > 0.")
        if method == "dog" and sigma_ratio <= 1:
            raise ValueError("sigma_ratio must be > 1 for Difference-of-Gaussians.")
        score = _score_image(
            image,
            method=method,
            sigma=sigma,
            sigma_ratio=sigma_ratio,
        )
        maxima = _local_maxima(
            score,
            threshold=threshold,
            min_distance=min_distance,
        )
        if len(maxima) > np.iinfo(np.uint32).max:
            raise ValueError("DetectSpots produced more labels than uint32 can store.")

        labels = np.zeros(image.shape, dtype=np.uint32)
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
        output_labels.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output_labels, labels)
        return [
            self.Outputs(
                output_labels=output_labels,
                spot_id=int(row["spot_id"]),
                y=float(row["y"]),
                x=float(row["x"]),
                intensity=float(row["intensity"]),
                score=float(row["score"]),
                spot_count=len(rows),
            )
            for row in rows
        ]
