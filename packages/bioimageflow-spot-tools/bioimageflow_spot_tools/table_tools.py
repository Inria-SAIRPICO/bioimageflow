"""Spot table filtering, rendering, colocalization, and quality metrics."""

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


def _read_rows(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output


def _float(row: dict[str, Any], column: str, default: float | None = None) -> float:
    value = row.get(column, "")
    if value in {"", None}:
        if default is None:
            raise ValueError(f"Spot table row is missing required column {column!r}.")
        return default
    return float(value)


def _int_id(row: dict[str, Any], column: str, default: int | None = None) -> int:
    value = row.get(column, "")
    if value in {"", None}:
        if default is None:
            raise ValueError(f"Spot table row is missing required column {column!r}.")
        return default
    return int(float(value))


def _argument(arguments: Arguments, name: str, default: Any) -> Any:
    return getattr(arguments, name, default)


def _parse_shape(shape: str) -> tuple[int, int]:
    parts = [int(part.strip()) for part in str(shape).replace("x", ",").split(",")]
    if len(parts) != 2 or min(parts) <= 0:
        raise ValueError("image_shape must be two positive integers, for example '128,128'.")
    return parts[0], parts[1]


def _shape_from_arguments(arguments: Arguments) -> tuple[int, int]:
    import imageio.v3 as iio

    reference_image = _argument(arguments, "reference_image", None)
    if reference_image is not None:
        image = iio.imread(reference_image)
        return int(image.shape[-2]), int(image.shape[-1])
    return _parse_shape(_argument(arguments, "image_shape", "256,256"))


def _spot_coordinate(
    row: dict[str, Any],
    *,
    shape: tuple[int, int] | None = None,
) -> tuple[float, float]:
    y = _float(row, "y")
    x = _float(row, "x")
    if shape is not None:
        iy = int(round(y))
        ix = int(round(x))
        if iy < 0 or iy >= shape[0] or ix < 0 or ix >= shape[1]:
            raise ValueError(
                f"Spot coordinate ({y}, {x}) is outside image bounds {shape}."
            )
    return y, x


def _draw_disk(image: Any, y: float, x: float, radius: int, value: int) -> None:
    radius = max(0, int(radius))
    cy = int(round(y))
    cx = int(round(x))
    for yy in range(max(0, cy - radius), min(image.shape[0], cy + radius + 1)):
        for xx in range(max(0, cx - radius), min(image.shape[1], cx + radius + 1)):
            if (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2:
                image[yy, xx] = value


class FilterSpots(ProcessingTool):
    """Filter spot tables by numeric columns and optional binary masks."""

    display_name = "Filter Spots"
    documentation = "Filter spot coordinate tables by intensity, score, radius, and mask."
    category = Category.SPOT_DETECTION
    tags = ["spots", "filter", "puncta"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        spots_csv: Annotated[
            Path,
            GUIMeta("Spots CSV", "Spot table with y and x columns.", connectable=Connectable.BY_DEFAULT),
        ]
        min_intensity: float | None = None
        max_intensity: float | None = None
        min_score: float | None = None
        max_score: float | None = None
        min_radius: float | None = None
        max_radius: float | None = None
        mask_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta("Mask image", "Optional nonzero mask for spot positions."),
        ] = None

    class Outputs(IOModel):
        filtered_spots_csv: Annotated[Path, GUIMeta("Filtered spots CSV")] = Template(
            "{spots_csv.stem}_filtered.csv"
        )
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        rows = _read_rows(arguments.spots_csv)
        mask_image = _argument(arguments, "mask_image", None)
        mask = iio.imread(mask_image) if mask_image is not None else None

        def keep(row: dict[str, str]) -> bool:
            checks = [
                (
                    "intensity",
                    _argument(arguments, "min_intensity", None),
                    _argument(arguments, "max_intensity", None),
                ),
                (
                    "score",
                    _argument(arguments, "min_score", None),
                    _argument(arguments, "max_score", None),
                ),
                (
                    "radius",
                    _argument(arguments, "min_radius", None),
                    _argument(arguments, "max_radius", None),
                ),
            ]
            for column, minimum, maximum in checks:
                if column not in row or row[column] in {"", None}:
                    continue
                value = float(row[column])
                if minimum is not None and value < float(minimum):
                    return False
                if maximum is not None and value > float(maximum):
                    return False
            if mask is not None:
                y_float, x_float = _spot_coordinate(row, shape=mask.shape[:2])
                y = int(round(y_float))
                x = int(round(x_float))
                if mask[y, x] == 0:
                    return False
            return True

        filtered = [row for row in rows if keep(row)]
        fieldnames = list(rows[0]) if rows else ["spot_id", "y", "x"]
        output = _write_rows(arguments.filtered_spots_csv, filtered, fieldnames)
        return self.Outputs(filtered_spots_csv=output, spot_count=len(filtered))


class RenderSpots(ProcessingTool):
    """Render spot coordinates into a 2D mask or label image."""

    display_name = "Render Spots"
    documentation = "Render coordinate tables to label or binary mask images."
    category = Category.SPOT_DETECTION
    tags = ["spots", "render", "labels"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        spots_csv: Annotated[Path, GUIMeta("Spots CSV", connectable=Connectable.BY_DEFAULT)]
        image_shape: str = "256,256"
        reference_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("Reference image", "Optional image used for output shape."),
        ] = None
        radius: int = 0
        label_mode: bool = True

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta("Rendered spots"),
        ] = Template("{spots_csv.stem}_rendered.tif")
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        rows = _read_rows(arguments.spots_csv)
        shape = _shape_from_arguments(arguments)
        image = np.zeros(shape, dtype=np.uint16)
        for index, row in enumerate(rows, start=1):
            value = _int_id(row, "spot_id", index) if _argument(arguments, "label_mode", True) else 1
            y, x = _spot_coordinate(row, shape=shape)
            _draw_disk(
                image,
                y,
                x,
                _argument(arguments, "radius", 0),
                value,
            )
        output = Path(arguments.output_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, image)
        return self.Outputs(output_image=output, spot_count=len(rows))


def _components(mask: Any) -> list[list[tuple[int, int]]]:
    import numpy as np

    seen = np.zeros(mask.shape, dtype=bool)
    components: list[list[tuple[int, int]]] = []
    for y, x in np.argwhere(mask):
        y = int(y)
        x = int(x)
        if seen[y, x]:
            continue
        stack = [(y, x)]
        seen[y, x] = True
        component = []
        while stack:
            cy, cx = stack.pop()
            component.append((cy, cx))
            for ny in range(max(0, cy - 1), min(mask.shape[0], cy + 2)):
                for nx in range(max(0, cx - 1), min(mask.shape[1], cx + 2)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        components.append(component)
    return components


class SpotsToLabels(ProcessingTool):
    """Create connected spot labels from coordinate tables or binary masks."""

    display_name = "Spots To Labels"
    documentation = "Convert spot coordinates or masks to connected label images."
    category = Category.SPOT_DETECTION
    tags = ["spots", "labels", "coordinates"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        spots_csv: Annotated[Path, GUIMeta("Spots CSV")] = None
        mask_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta("Spot mask"),
        ] = None
        image_shape: str = "256,256"
        radius: int = 0

    class Outputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta("Spot labels"),
        ] = Template("spots_labels.tif")
        label_count: Annotated[int, GUIMeta("Label count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        mask_image = _argument(arguments, "mask_image", None)
        spots_csv = _argument(arguments, "spots_csv", None)
        if mask_image is not None:
            mask = iio.imread(mask_image) > 0
            labels = np.zeros(mask.shape, dtype=np.uint16)
            components = _components(mask)
            for label, component in enumerate(components, start=1):
                for y, x in component:
                    labels[y, x] = label
            label_count = len(components)
        elif spots_csv is not None:
            rows = _read_rows(spots_csv)
            shape = _parse_shape(_argument(arguments, "image_shape", "256,256"))
            labels = np.zeros(shape, dtype=np.uint16)
            for index, row in enumerate(rows, start=1):
                value = _int_id(row, "spot_id", index)
                y, x = _spot_coordinate(row, shape=shape)
                _draw_disk(
                    labels,
                    y,
                    x,
                    _argument(arguments, "radius", 0),
                    value,
                )
            label_count = len(rows)
        else:
            raise ValueError("SpotsToLabels requires spots_csv or mask_image.")

        output = Path(arguments.label_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, labels)
        return self.Outputs(label_image=output, label_count=label_count)


class SpotColocalization(ProcessingTool):
    """Match two spot tables with a nearest-neighbor distance threshold."""

    display_name = "Spot Colocalization"
    documentation = "Match spots between channels within a distance threshold."
    category = Category.COLOCALIZATION
    tags = ["spots", "colocalization", "matching"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        reference_spots_csv: Annotated[Path, GUIMeta("Reference spots CSV")]
        query_spots_csv: Annotated[Path, GUIMeta("Query spots CSV")]
        max_distance: float = 2.0

    class Outputs(IOModel):
        matches_csv: Annotated[Path, GUIMeta("Matches CSV")] = Template("spot_matches.csv")
        matched_count: Annotated[int, GUIMeta("Matched spots")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np

        reference = _read_rows(arguments.reference_spots_csv)
        query = _read_rows(arguments.query_spots_csv)
        used_query: set[int] = set()
        matches = []
        for reference_index, ref in enumerate(reference, start=1):
            best_index = None
            best_distance = float("inf")
            for query_index, candidate in enumerate(query, start=1):
                if query_index in used_query:
                    continue
                distance = float(
                    np.hypot(
                        _float(ref, "y") - _float(candidate, "y"),
                        _float(ref, "x") - _float(candidate, "x"),
                    )
                )
                if distance <= float(arguments.max_distance) and distance < best_distance:
                    best_index = query_index
                    best_distance = distance
            if best_index is None:
                continue
            used_query.add(best_index)
            query_row = query[best_index - 1]
            matches.append(
                {
                    "reference_spot_id": _int_id(ref, "spot_id", reference_index),
                    "query_spot_id": _int_id(query_row, "spot_id", best_index),
                    "distance": best_distance,
                }
            )
        output = _write_rows(
            arguments.matches_csv,
            matches,
            ["reference_spot_id", "query_spot_id", "distance"],
        )
        return self.Outputs(matches_csv=output, matched_count=len(matches))


class SpotQualityMetrics(ProcessingTool):
    """Compute local spot quality metrics from an image and spot table."""

    display_name = "Spot Quality Metrics"
    documentation = "Compute SNR, local background, and nearest-neighbor distances."
    category = Category.MEASUREMENT
    tags = ["spots", "quality", "snr"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        spots_csv: Annotated[Path, GUIMeta("Spots CSV", connectable=Connectable.BY_DEFAULT)]
        image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("Intensity image", connectable=Connectable.BY_DEFAULT),
        ]
        radius: int = 2

    class Outputs(IOModel):
        metrics_csv: Annotated[Path, GUIMeta("Spot quality metrics")] = Template(
            "{spots_csv.stem}_quality.csv"
        )
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        rows = _read_rows(arguments.spots_csv)
        image = iio.imread(arguments.image).astype(np.float32)
        coordinates = [_spot_coordinate(row, shape=image.shape[:2]) for row in rows]
        metrics = []
        radius = max(1, int(_argument(arguments, "radius", 2)))
        for index, row in enumerate(rows, start=1):
            y_float, x_float = coordinates[index - 1]
            y = int(round(y_float))
            x = int(round(x_float))
            y0 = max(0, y - radius)
            y1 = min(image.shape[0], y + radius + 1)
            x0 = max(0, x - radius)
            x1 = min(image.shape[1], x + radius + 1)
            window = image[y0:y1, x0:x1]
            background = float(np.median(window)) if window.size else 0.0
            noise = float(np.std(window)) if window.size else 0.0
            intensity = _float(row, "intensity", float(image[y, x]))
            distances = [
                float(np.hypot(y - other_y, x - other_x))
                for other_y, other_x in coordinates
                if (other_y, other_x) != coordinates[index - 1]
            ]
            nearest = min(distances) if distances else 0.0
            metrics.append(
                {
                    **row,
                    "local_background": background,
                    "snr": (intensity - background) / (noise if noise > 0 else 1.0),
                    "nearest_neighbor_distance": nearest,
                }
            )
        fieldnames = list(rows[0]) if rows else ["spot_id", "y", "x"]
        fieldnames += ["local_background", "snr", "nearest_neighbor_distance"]
        output = _write_rows(arguments.metrics_csv, metrics, fieldnames)
        return self.Outputs(metrics_csv=output, spot_count=len(metrics))
