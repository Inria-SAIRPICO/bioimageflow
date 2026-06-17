"""Spot table filtering, rendering, colocalization, and quality metrics."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

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

if TYPE_CHECKING:
    from bioimageflow import DataFrameTool as DataFrameTool
    from bioimageflow import Passthrough as Passthrough
else:
    try:
        from bioimageflow import DataFrameTool as DataFrameTool
        from bioimageflow import Passthrough as Passthrough
    except ModuleNotFoundError:
        class DataFrameTool:  # type: ignore[no-redef]
            """Unavailable outside the orchestrator environment."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError(
                    "DataFrameTool classes require the bioimageflow orchestrator package."
                )

        class Passthrough(IOModel):  # type: ignore[no-redef]
            """Fallback schema base for worker-only imports."""


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


def _positive_uint32_id(row: dict[str, Any], column: str, default: int | None = None) -> int:
    import numpy as np

    raw_value = row.get(column, "")
    if raw_value in {"", None}:
        if default is None:
            raise ValueError(f"Spot table row is missing required column {column!r}.")
        raw_value = default
    numeric_value = float(raw_value)
    if not np.isfinite(numeric_value) or not numeric_value.is_integer():
        raise ValueError(f"{column} must be a positive integer; 0 is reserved for background.")
    value = int(numeric_value)
    if value <= 0:
        raise ValueError(f"{column} must be a positive integer; 0 is reserved for background.")
    if value > np.iinfo(np.uint32).max:
        raise ValueError(f"{column} must be <= {np.iinfo(np.uint32).max}.")
    return value


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


def _has_spot_coordinate(arguments: Arguments) -> bool:
    return _argument(arguments, "y", None) is not None or _argument(arguments, "x", None) is not None


def _draw_disk(image: Any, y: float, x: float, radius: int, value: int) -> None:
    radius = max(0, int(radius))
    cy = int(round(y))
    cx = int(round(x))
    for yy in range(max(0, cy - radius), min(image.shape[0], cy + radius + 1)):
        for xx in range(max(0, cx - radius), min(image.shape[1], cx + radius + 1)):
            if (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2:
                image[yy, xx] = value


def _blank_rendered_spots_output(tool: "RenderSpots", arguments: Arguments) -> list[Any]:
    import imageio.v3 as iio
    import numpy as np

    shape = _shape_from_arguments(arguments)
    label_mode = bool(_argument(arguments, "label_mode", True))
    image = np.zeros(shape, dtype=np.uint32 if label_mode else np.uint8)
    output = Path(arguments.output_image)
    output.parent.mkdir(parents=True, exist_ok=True)
    iio.imwrite(output, image)
    return [tool.Outputs(output_image=output, spot_count=0)]


class FilterSpots(DataFrameTool):
    """Filter spot tables by numeric columns and optional binary masks."""

    display_name = "Filter Spots"
    documentation = "Filter spot coordinate tables by intensity, score, radius, and mask."
    category = Category.SPOT_DETECTION
    tags = ["spots", "filter", "puncta"]

    class Inputs(IOModel):
        min_intensity: float | None = None
        max_intensity: float | None = None
        min_score: float | None = None
        max_score: float | None = None
        min_radius: float | None = None
        max_radius: float | None = None
        mask_image: Annotated[
            Path | None,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta("Mask image", "Optional nonzero mask for spot positions."),
        ] = None

    class Outputs(Passthrough):
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def merge_dataframes(self, dfs: list[Any], arguments: Arguments) -> Any:
        if len(dfs) != 1:
            raise ValueError("FilterSpots requires exactly one upstream spot table.")
        return dfs[0].copy()

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import imageio.v3 as iio
        import pandas as pd

        mask_image = _argument(arguments, "mask_image", None)
        mask = iio.imread(mask_image) if mask_image is not None else None
        keep = pd.Series(True, index=df.index)
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
            if minimum is None and maximum is None:
                continue
            if column not in df.columns:
                raise ValueError(
                    f"FilterSpots input table is missing required column {column!r}."
                )
            values = cast(pd.Series, pd.to_numeric(df[column]))
            if minimum is not None:
                keep &= values >= float(minimum)
            if maximum is not None:
                keep &= values <= float(maximum)

        if mask is not None:
            missing = {"y", "x"} - set(df.columns)
            if missing:
                raise ValueError(
                    "FilterSpots input table is missing required column(s): "
                    + ", ".join(repr(column) for column in sorted(missing))
                    + "."
                )
            mask_keep = []
            for _, row in df.iterrows():
                y_float, x_float = _spot_coordinate(row.to_dict(), shape=mask.shape[:2])
                y = int(round(y_float))
                x = int(round(x_float))
                mask_keep.append(mask[y, x] != 0)
            keep &= pd.Series(mask_keep, index=df.index)

        filtered = df.loc[keep].copy()
        filtered["spot_count"] = len(filtered)
        return filtered


class RenderSpots(ProcessingTool):
    """Render spot coordinates into a 2D mask or label image."""

    display_name = "Render Spots"
    documentation = "Render coordinate tables to label or binary mask images."
    category = Category.SPOT_DETECTION
    tags = ["spots", "render", "labels"]
    environment = GENERAL_ENV
    run_empty_batch = True
    empty_batch_anchor_inputs = ("reference_image",)

    class Inputs(IOModel):
        spot_id: Annotated[int, GUIMeta("Spot ID", connectable=Connectable.BY_DEFAULT)]
        y: Annotated[float, GUIMeta("Y", connectable=Connectable.BY_DEFAULT)]
        x: Annotated[float, GUIMeta("X", connectable=Connectable.BY_DEFAULT)]
        image_shape: str = "256,256"
        reference_image: Annotated[
            Path | None,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("Reference image", "Optional image used for output shape."),
        ] = None
        radius: int = 0
        label_mode: bool = True

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.BINARY, Semantic.LABEL},
                layouts={Layout.PLANAR},
                dtypes={"uint8", "uint32"},
            ),
            GUIMeta("Rendered spots"),
        ] = Template("rendered_spots.tif")
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def process_batch(
        self,
        arguments_list: list[Arguments],
        *,
        context: Any = None,
    ) -> Any:
        import imageio.v3 as iio
        import numpy as np

        if not arguments_list:
            return []
        arguments = arguments_list[0]
        shape = _shape_from_arguments(arguments)
        label_mode = bool(_argument(arguments, "label_mode", True))
        image = np.zeros(shape, dtype=np.uint32 if label_mode else np.uint8)
        rows = [
            row_arguments
            for row_arguments in arguments_list
            if _has_spot_coordinate(row_arguments)
        ]
        if not rows:
            return [
                _blank_rendered_spots_output(self, arguments)
                for arguments in arguments_list
            ]
        for index, row_arguments in enumerate(rows, start=1):
            row = {
                "spot_id": _argument(row_arguments, "spot_id", index),
                "y": _argument(row_arguments, "y", None),
                "x": _argument(row_arguments, "x", None),
            }
            value = _positive_uint32_id(row, "spot_id", index) if label_mode else 1
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
        return [[self.Outputs(output_image=output, spot_count=len(rows))]]


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
    run_empty_batch = True

    class Inputs(IOModel):
        spot_id: Annotated[int | None, GUIMeta("Spot ID", connectable=Connectable.BY_DEFAULT)] = None
        y: Annotated[float | None, GUIMeta("Y", connectable=Connectable.BY_DEFAULT)] = None
        x: Annotated[float | None, GUIMeta("X", connectable=Connectable.BY_DEFAULT)] = None
        mask_image: Annotated[
            Path | None,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta("Spot mask"),
        ] = None
        image_shape: str = "256,256"
        radius: int = 0

    class Outputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}, dtypes={"uint32"}),
            GUIMeta("Spot labels"),
        ] = Template("spots_labels.tif")
        label_count: Annotated[int, GUIMeta("Label count")]

    def _blank_coordinate_outputs(self, arguments_list: list[Arguments]) -> list[list[Any]]:
        import imageio.v3 as iio
        import numpy as np

        outputs = []
        for arguments in arguments_list:
            shape = _parse_shape(_argument(arguments, "image_shape", "256,256"))
            labels = np.zeros(shape, dtype=np.uint32)
            output = Path(arguments.label_image)
            output.parent.mkdir(parents=True, exist_ok=True)
            iio.imwrite(output, labels)
            outputs.append([self.Outputs(label_image=output, label_count=0)])
        return outputs

    def process_batch(
        self,
        arguments_list: list[Arguments],
        *,
        context: Any = None,
    ) -> Any:
        import imageio.v3 as iio
        import numpy as np

        if not arguments_list:
            return []
        arguments = arguments_list[0]
        mask_image = _argument(arguments, "mask_image", None)
        if mask_image is not None:
            outputs = []
            for row_arguments in arguments_list:
                mask = iio.imread(_argument(row_arguments, "mask_image", None)) > 0
                labels = np.zeros(mask.shape, dtype=np.uint32)
                components = _components(mask)
                if len(components) > np.iinfo(np.uint32).max:
                    raise ValueError("SpotsToLabels produced more labels than uint32 can store.")
                for label, component in enumerate(components, start=1):
                    for y, x in component:
                        labels[y, x] = label
                output = Path(row_arguments.label_image)
                output.parent.mkdir(parents=True, exist_ok=True)
                iio.imwrite(output, labels)
                outputs.append([
                    self.Outputs(label_image=output, label_count=len(components))
                ])
            return outputs
        else:
            rows = [
                {
                    "spot_id": _argument(row_arguments, "spot_id", index),
                    "y": _argument(row_arguments, "y", None),
                    "x": _argument(row_arguments, "x", None),
                }
                for index, row_arguments in enumerate(arguments_list, start=1)
                if _has_spot_coordinate(row_arguments)
            ]
            if not rows:
                return self._blank_coordinate_outputs(arguments_list)
            shape = _parse_shape(_argument(arguments, "image_shape", "256,256"))
            labels = np.zeros(shape, dtype=np.uint32)
            for index, row in enumerate(rows, start=1):
                value = _positive_uint32_id(row, "spot_id", index)
                y, x = _spot_coordinate(row, shape=shape)
                _draw_disk(
                    labels,
                    y,
                    x,
                    _argument(arguments, "radius", 0),
                    value,
                )
            label_count = len(rows)

            output = Path(arguments.label_image)
            output.parent.mkdir(parents=True, exist_ok=True)
            iio.imwrite(output, labels)
            return [[self.Outputs(label_image=output, label_count=label_count)]]


class SpotColocalization(DataFrameTool):
    """Match two spot tables with a nearest-neighbor distance threshold."""

    display_name = "Spot Colocalization"
    documentation = "Match spots between channels within a distance threshold."
    category = Category.COLOCALIZATION
    tags = ["spots", "colocalization", "matching"]

    class Inputs(IOModel):
        group_by: Annotated[
            str | None,
            GUIMeta(
                "Group by",
                "Optional column used to match spots independently per image or field.",
            ),
        ] = None
        max_distance: float = 2.0

    class Outputs(IOModel):
        group: Annotated[str, GUIMeta("Group")]
        reference_spot_id: Annotated[int, GUIMeta("Reference spot ID")]
        query_spot_id: Annotated[int, GUIMeta("Query spot ID")]
        distance: Annotated[float, GUIMeta("Distance")]
        matched_count: Annotated[int, GUIMeta("Matched spots")]

    def merge_dataframes(
        self,
        dfs: list[Any],
        arguments: Arguments,
    ) -> Any:
        import numpy as np
        import pandas as pd

        if len(dfs) != 2:
            raise ValueError(
                "SpotColocalization requires exactly two upstream spot tables: "
                "reference first, query second."
            )

        reference_df, query_df = (df.copy() for df in dfs)
        group_by = _argument(arguments, "group_by", None)
        max_distance = float(_argument(arguments, "max_distance", 2.0))

        self._validate_spot_table(reference_df, "reference", group_by)
        self._validate_spot_table(query_df, "query", group_by)

        reference_groups = self._group_spots(reference_df, group_by)
        query_groups = self._group_spots(query_df, group_by)
        shared_groups = sorted(set(reference_groups) & set(query_groups))
        if not shared_groups and (len(reference_df) > 0 or len(query_df) > 0):
            raise ValueError(
                "SpotColocalization found no shared groups between reference and "
                "query tables. Provide group_by when the tables do not share "
                "BioImageFlow index lineage."
            )

        output_rows: list[dict[str, Any]] = []
        output_index: list[str] = []
        for group in shared_groups:
            reference_rows = reference_groups[group]
            query_rows = query_groups[group]
            used_query: set[int] = set()
            group_matches: list[dict[str, Any]] = []
            for reference_index, ref in enumerate(reference_rows, start=1):
                best_index = None
                best_distance = float("inf")
                for query_index, candidate in enumerate(query_rows, start=1):
                    if query_index in used_query:
                        continue
                    distance = float(
                        np.hypot(
                            _float(ref, "y") - _float(candidate, "y"),
                            _float(ref, "x") - _float(candidate, "x"),
                        )
                    )
                    if distance <= max_distance and distance < best_distance:
                        best_index = query_index
                        best_distance = distance
                if best_index is None:
                    continue
                used_query.add(best_index)
                query_row = query_rows[best_index - 1]
                group_matches.append(
                    {
                        "group": group,
                        "reference_spot_id": _int_id(ref, "spot_id", reference_index),
                        "query_spot_id": _int_id(query_row, "spot_id", best_index),
                        "distance": best_distance,
                    }
                )

            matched_count = len(group_matches)
            for match_index, match in enumerate(group_matches):
                match["matched_count"] = matched_count
                output_rows.append(match)
                output_index.append(f"{group}::{match_index}")

        columns = [
            "group",
            "reference_spot_id",
            "query_spot_id",
            "distance",
            "matched_count",
        ]
        return pd.DataFrame(
            output_rows,
            columns=pd.Index(columns),
            index=pd.Index(output_index),
        )

    @staticmethod
    def _validate_spot_table(df: Any, role: str, group_by: str | None) -> None:
        required = {"spot_id", "y", "x"}
        if group_by is not None:
            required.add(group_by)
        missing = sorted(required - set(df.columns))
        if missing:
            raise ValueError(
                f"SpotColocalization {role} table is missing required "
                f"column(s): {', '.join(repr(column) for column in missing)}."
            )

    @staticmethod
    def _group_spots(df: Any, group_by: str | None) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for index, row in df.iterrows():
            if group_by is None:
                group = str(index).split("::", 1)[0]
            else:
                group = str(row[group_by])
            groups.setdefault(group, []).append(row.to_dict())
        return groups


class SpotQualityMetrics(DataFrameTool):
    """Compute local spot quality metrics from an image and spot table."""

    display_name = "Spot Quality Metrics"
    documentation = "Compute SNR, local background, and nearest-neighbor distances."
    category = Category.MEASUREMENT
    tags = ["spots", "quality", "snr"]

    class Inputs(IOModel):
        image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("Intensity image"),
        ]
        radius: int = 2

    class Outputs(Passthrough):
        local_background: Annotated[float, GUIMeta("Local background")]
        snr: Annotated[float, GUIMeta("SNR")]
        nearest_neighbor_distance: Annotated[float, GUIMeta("Nearest neighbor distance")]
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def merge_dataframes(self, dfs: list[Any], arguments: Arguments) -> Any:
        if len(dfs) != 1:
            raise ValueError(
                "SpotQualityMetrics requires exactly one upstream spot table."
            )
        return dfs[0].copy()

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import imageio.v3 as iio
        import numpy as np

        missing = {"y", "x"} - set(df.columns)
        if missing:
            raise ValueError(
                "SpotQualityMetrics input table is missing required column(s): "
                + ", ".join(repr(column) for column in sorted(missing))
                + "."
            )

        result = df.copy()
        if "spot_id" not in result.columns:
            result["spot_id"] = range(1, len(result) + 1)
        image = iio.imread(arguments.image).astype(np.float32)
        coordinates = [
            _spot_coordinate(row.to_dict(), shape=image.shape[:2])
            for _, row in result.iterrows()
        ]
        metrics = []
        radius = max(1, int(_argument(arguments, "radius", 2)))
        for index, (_, row) in enumerate(result.iterrows(), start=1):
            row_dict = row.to_dict()
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
            intensity = _float(row_dict, "intensity", float(image[y, x]))
            distances = [
                float(np.hypot(y - other_y, x - other_x))
                for other_y, other_x in coordinates
                if (other_y, other_x) != coordinates[index - 1]
            ]
            nearest = min(distances) if distances else 0.0
            metrics.append(
                {
                    "local_background": background,
                    "snr": (intensity - background) / (noise if noise > 0 else 1.0),
                    "nearest_neighbor_distance": nearest,
                }
            )
        result["local_background"] = [
            metric["local_background"] for metric in metrics
        ]
        result["snr"] = [metric["snr"] for metric in metrics]
        result["nearest_neighbor_distance"] = [
            metric["nearest_neighbor_distance"] for metric in metrics
        ]
        result["spot_count"] = len(result)
        return result
