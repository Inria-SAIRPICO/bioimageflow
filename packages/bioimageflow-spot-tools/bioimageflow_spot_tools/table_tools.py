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
    RowConsumption,
    Semantic,
    Template,
)

from .validation import (
    finite_float,
    integral_value,
    pixel_coordinate,
    planar_array,
    positive_uint32_id,
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


def _row_float(row: dict[str, Any], column: str, default: float | None = None) -> float:
    value = row.get(column, "")
    if value in {"", None}:
        if default is None:
            raise ValueError(f"Spot table row is missing required column {column!r}.")
        return default
    return finite_float(value, column)


def _parse_shape(shape: str) -> tuple[int, int]:
    raw_parts = str(shape).replace("x", ",").split(",")
    if len(raw_parts) != 2:
        raise ValueError(
            "image_shape must be two positive integers, for example '128,128'."
        )
    try:
        parts = [
            integral_value(part.strip(), "image_shape", minimum=1) for part in raw_parts
        ]
    except ValueError as error:
        raise ValueError(
            "image_shape must be two positive integers, for example '128,128'."
        ) from error
    return parts[0], parts[1]


def _shape_from_arguments(arguments: Arguments) -> tuple[int, int]:
    import imageio.v3 as iio

    reference_image = getattr(arguments, "reference_image", None)
    if reference_image is not None:
        image = planar_array(iio.imread(reference_image), "reference_image")
        return int(image.shape[0]), int(image.shape[1])
    return _parse_shape(getattr(arguments, "image_shape", "256,256"))


def _has_spot_coordinate(arguments: Arguments) -> bool:
    return (
        getattr(arguments, "y", None) is not None
        or getattr(arguments, "x", None) is not None
    )


def _draw_disk(image: Any, y: int, x: int, radius: int, value: int) -> None:
    """Draw a clipped disk including pixels exactly on its radius."""
    import numpy as np

    y0 = max(0, y - radius)
    y1 = min(image.shape[0], y + radius + 1)
    x0 = max(0, x - radius)
    x1 = min(image.shape[1], x + radius + 1)
    yy, xx = np.ogrid[y0:y1, x0:x1]
    region = image[y0:y1, x0:x1]
    region[(yy - y) ** 2 + (xx - x) ** 2 <= radius**2] = value


def _rendered_label_count(image: Any) -> int:
    import numpy as np

    return int(np.count_nonzero(np.unique(image)))


def _blank_rendered_spots_output(
    tool: "RenderSpots", arguments: Arguments
) -> list[Any]:
    import imageio.v3 as iio
    import numpy as np

    shape = _shape_from_arguments(arguments)
    label_mode = bool(getattr(arguments, "label_mode", True))
    image = np.zeros(shape, dtype=np.uint32 if label_mode else np.uint8)
    output = Path(arguments.output_image)
    output.parent.mkdir(parents=True, exist_ok=True)
    iio.imwrite(output, image)
    return [tool.Outputs(output_image=output, spot_count=0)]


class FilterSpots(DataFrameTool):
    """Filter spot tables by numeric columns and optional binary masks."""

    display_name = "Filter Spots"
    documentation = (
        "Filter spot coordinate tables by intensity, score, radius, and mask."
    )
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

        import numpy as np

        mask_image = getattr(arguments, "mask_image", None)
        mask = (
            planar_array(iio.imread(mask_image), "mask_image")
            if mask_image is not None
            else None
        )
        keep = pd.Series(True, index=df.index)
        checks = [
            (
                "intensity",
                getattr(arguments, "min_intensity", None),
                getattr(arguments, "max_intensity", None),
            ),
            (
                "score",
                getattr(arguments, "min_score", None),
                getattr(arguments, "max_score", None),
            ),
            (
                "radius",
                getattr(arguments, "min_radius", None),
                getattr(arguments, "max_radius", None),
            ),
        ]
        for column, minimum, maximum in checks:
            if minimum is None and maximum is None:
                continue
            if column not in df.columns:
                raise ValueError(
                    f"FilterSpots input table is missing required column {column!r}."
                )
            try:
                values = cast(pd.Series, pd.to_numeric(df[column], errors="raise"))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"FilterSpots column {column!r} must be numeric."
                ) from error
            if not np.all(np.isfinite(values.to_numpy(dtype=float))):
                raise ValueError(
                    f"FilterSpots column {column!r} must contain finite values."
                )
            lower = (
                finite_float(minimum, f"min_{column}") if minimum is not None else None
            )
            upper = (
                finite_float(maximum, f"max_{column}") if maximum is not None else None
            )
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"min_{column} must be <= max_{column}.")
            if minimum is not None:
                keep &= values >= lower
            if maximum is not None:
                keep &= values <= upper

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
                row_dict = row.to_dict()
                _, _, y, x = pixel_coordinate(
                    row_dict.get("y"), row_dict.get("x"), mask.shape
                )
                mask_keep.append(mask[y, x] != 0)
            keep &= pd.Series(mask_keep, index=df.index)

        filtered = df.loc[keep].copy()
        filtered["spot_count"] = len(filtered)
        return filtered


class RenderSpots(ProcessingTool):
    """Render spot coordinates into a 2D mask or label image."""

    row_consumption = RowConsumption.COLLECTIVE
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
        if not arguments_list:
            return []
        if not any(_has_spot_coordinate(arguments) for arguments in arguments_list):
            return [
                _blank_rendered_spots_output(self, arguments)
                for arguments in arguments_list
            ]

        grouped_positions: dict[Path | None, list[int]] = {}
        for position, arguments in enumerate(arguments_list):
            reference = getattr(arguments, "reference_image", None)
            source = Path(reference) if reference is not None else None
            grouped_positions.setdefault(source, []).append(position)

        output_sources: dict[Path, Path | None] = {}
        rendered: list[list[Any]] = [[] for _ in arguments_list]
        for source, positions in grouped_positions.items():
            rows = [arguments_list[position] for position in positions]
            outputs = {Path(arguments.output_image) for arguments in rows}
            if len(outputs) != 1:
                raise ValueError(
                    "RenderSpots rows for one reference_image must reference the "
                    "same output_image."
                )
            output = next(iter(outputs))
            previous_source = output_sources.setdefault(output, source)
            if previous_source != source:
                raise ValueError(
                    "RenderSpots cannot write multiple reference images to the "
                    "same output_image. Use an output template containing "
                    "{reference_image.stem}."
                )

            coordinate_rows = [row for row in rows if _has_spot_coordinate(row)]
            if coordinate_rows:
                group_output = self._render_group(coordinate_rows)
            else:
                group_output = _blank_rendered_spots_output(self, rows[0])
            for position in positions:
                rendered[position] = group_output
        return rendered

    def _render_group(self, rows: list[Arguments]) -> list[Any]:
        import imageio.v3 as iio
        import numpy as np

        arguments = rows[0]
        shape = _shape_from_arguments(arguments)
        label_mode = bool(getattr(arguments, "label_mode", True))
        radius = integral_value(getattr(arguments, "radius", 0), "radius", minimum=0)
        output = Path(arguments.output_image)

        for row in rows[1:]:
            row_shape = _shape_from_arguments(row)
            row_label_mode = bool(getattr(row, "label_mode", True))
            row_radius = integral_value(getattr(row, "radius", 0), "radius", minimum=0)
            if (row_shape, row_label_mode, row_radius) != (
                shape,
                label_mode,
                radius,
            ):
                raise ValueError(
                    "RenderSpots rows for one reference_image must use the same "
                    "shape, label_mode, and radius."
                )

        image = np.zeros(shape, dtype=np.uint32 if label_mode else np.uint8)
        for index, row in enumerate(rows, start=1):
            spot_id = getattr(row, "spot_id", None)
            if spot_id is None:
                spot_id = index
            value = positive_uint32_id(spot_id) if label_mode else 1
            _, _, y, x = pixel_coordinate(
                getattr(row, "y", None),
                getattr(row, "x", None),
                shape,
            )
            _draw_disk(image, y, x, radius, value)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, image)
        spot_count = _rendered_label_count(image) if label_mode else len(rows)
        return [self.Outputs(output_image=output, spot_count=spot_count)]


class MaskToLabels(ProcessingTool):
    """Create connected-component labels from one binary mask row."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Mask To Labels"
    documentation = "Convert each binary mask into a connected label image."
    category = Category.SPOT_DETECTION
    tags = ["mask", "labels", "components"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        mask_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.BINARY}, layouts={Layout.PLANAR}),
            GUIMeta("Spot mask", "A 2D mask where every nonzero pixel is foreground."),
        ]

    class Outputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
                dtypes={"uint32"},
            ),
            GUIMeta("Spot labels"),
        ] = Template("{mask_image.stem}_labels.tif")
        label_count: Annotated[int, GUIMeta("Label count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        from skimage.measure import label

        mask = planar_array(iio.imread(arguments.mask_image), "mask_image") != 0
        labels, label_count = cast(
            tuple[Any, int],
            label(
                mask,
                background=0,
                connectivity=2,
                return_num=True,
            ),
        )
        import numpy as np

        if label_count > np.iinfo(np.uint32).max:
            raise ValueError("MaskToLabels produced more labels than uint32 can store.")
        labels = labels.astype(np.uint32, copy=False)
        output = Path(arguments.label_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, labels)
        return self.Outputs(label_image=output, label_count=int(label_count))


class SpotsToLabels(ProcessingTool):
    """Create one label image from a table of spot coordinates."""

    row_consumption = RowConsumption.COLLECTIVE
    display_name = "Spots To Labels"
    documentation = "Convert a spot coordinate table into one label image."
    category = Category.SPOT_DETECTION
    tags = ["spots", "labels", "coordinates"]
    environment = GENERAL_ENV
    run_empty_batch = True

    class Inputs(IOModel):
        spot_id: Annotated[
            int | None, GUIMeta("Spot ID", connectable=Connectable.BY_DEFAULT)
        ] = None
        y: Annotated[float | None, GUIMeta("Y", connectable=Connectable.BY_DEFAULT)] = (
            None
        )
        x: Annotated[float | None, GUIMeta("X", connectable=Connectable.BY_DEFAULT)] = (
            None
        )
        image_shape: str = "256,256"
        radius: int = 0

    class Outputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL}, layouts={Layout.PLANAR}, dtypes={"uint32"}
            ),
            GUIMeta("Spot labels"),
        ] = Template("spots_labels.tif")
        label_count: Annotated[int, GUIMeta("Label count")]

    def _blank_coordinate_output(self, arguments: Arguments) -> list[Any]:
        import imageio.v3 as iio
        import numpy as np

        shape = _parse_shape(getattr(arguments, "image_shape", "256,256"))
        labels = np.zeros(shape, dtype=np.uint32)
        output = Path(arguments.label_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, labels)
        return [self.Outputs(label_image=output, label_count=0)]

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
        shape = _parse_shape(getattr(arguments, "image_shape", "256,256"))
        radius = integral_value(getattr(arguments, "radius", 0), "radius", minimum=0)
        output = Path(arguments.label_image)
        for row_arguments in arguments_list[1:]:
            row_shape = _parse_shape(
                getattr(row_arguments, "image_shape", "256,256")
            )
            row_radius = integral_value(
                getattr(row_arguments, "radius", 0), "radius", minimum=0
            )
            row_output = Path(row_arguments.label_image)
            if (row_shape, row_radius, row_output) != (shape, radius, output):
                raise ValueError(
                    "SpotsToLabels rows in one collective batch must use the same "
                    "image_shape, radius, and label_image."
                )
        rows = [
            row_arguments
            for row_arguments in arguments_list
            if _has_spot_coordinate(row_arguments)
        ]
        if not rows:
            blank_output = self._blank_coordinate_output(arguments)
            return [blank_output for _ in arguments_list]
        labels = np.zeros(shape, dtype=np.uint32)
        for index, row_arguments in enumerate(rows, start=1):
            spot_id = getattr(row_arguments, "spot_id", None)
            if spot_id is None:
                spot_id = index
            value = positive_uint32_id(spot_id)
            _, _, y, x = pixel_coordinate(
                getattr(row_arguments, "y", None),
                getattr(row_arguments, "x", None),
                shape,
            )
            _draw_disk(labels, y, x, radius, value)
        label_count = _rendered_label_count(labels)

        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, labels)
        return [
            [self.Outputs(label_image=output, label_count=label_count)]
            for _ in arguments_list
        ]


class SpotColocalization(DataFrameTool):
    """Compute a global one-to-one matching between two spot tables."""

    display_name = "Spot Colocalization"
    documentation = (
        "Globally match spots between channels, maximizing the number of pairs "
        "within a distance threshold before minimizing their total distance."
    )
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
        import pandas as pd

        if len(dfs) != 2:
            raise ValueError(
                "SpotColocalization requires exactly two upstream spot tables: "
                "reference first, query second."
            )

        reference_df, query_df = (df.copy() for df in dfs)
        group_by = getattr(arguments, "group_by", None)
        max_distance = finite_float(
            getattr(arguments, "max_distance", 2.0), "max_distance"
        )
        if max_distance < 0:
            raise ValueError("max_distance must be >= 0.")

        self._validate_spot_table(reference_df, "reference", group_by)
        self._validate_spot_table(query_df, "query", group_by)

        reference_groups = self._group_spots(reference_df, group_by)
        query_groups = self._group_spots(query_df, group_by)
        shared_groups = sorted(set(reference_groups) & set(query_groups))
        if (
            not shared_groups
            and len(reference_df) > 0
            and len(query_df) > 0
        ):
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
            group_matches: list[dict[str, Any]] = []
            for reference_index, query_index, distance in self._match_group(
                reference_rows,
                query_rows,
                max_distance,
            ):
                ref = reference_rows[reference_index]
                query_row = query_rows[query_index]
                group_matches.append(
                    {
                        "group": group,
                        "reference_spot_id": positive_uint32_id(ref["spot_id"]),
                        "query_spot_id": positive_uint32_id(query_row["spot_id"]),
                        "distance": distance,
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
        import pandas as pd

        required = {"spot_id", "y", "x"}
        if group_by is not None:
            required.add(group_by)
        missing = sorted(required - set(df.columns))
        if missing:
            raise ValueError(
                f"SpotColocalization {role} table is missing required "
                f"column(s): {', '.join(repr(column) for column in missing)}."
            )
        for row_number, (_, row) in enumerate(df.iterrows(), start=1):
            positive_uint32_id(row["spot_id"], f"{role} spot_id at row {row_number}")
            finite_float(row["y"], f"{role} y at row {row_number}")
            finite_float(row["x"], f"{role} x at row {row_number}")
            if group_by is not None and pd.isna(row[group_by]):
                raise ValueError(
                    f"SpotColocalization {role} table column {group_by!r} "
                    "must not contain missing groups."
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
        for group, rows in groups.items():
            spot_ids = [positive_uint32_id(row["spot_id"]) for row in rows]
            if len(spot_ids) != len(set(spot_ids)):
                raise ValueError(
                    "SpotColocalization spot_id values must be unique within "
                    f"group {group!r}."
                )
        return groups

    @staticmethod
    def _match_group(
        reference_rows: list[dict[str, Any]],
        query_rows: list[dict[str, Any]],
        max_distance: float,
    ) -> list[tuple[int, int, float]]:
        """Maximize cardinality, then minimize total Euclidean distance."""
        import numpy as np
        from scipy.optimize import linear_sum_assignment
        from scipy.spatial.distance import cdist

        if not reference_rows or not query_rows:
            return []

        reference = np.asarray(
            [
                [finite_float(row["y"], "y"), finite_float(row["x"], "x")]
                for row in reference_rows
            ]
        )
        query = np.asarray(
            [
                [finite_float(row["y"], "y"), finite_float(row["x"], "x")]
                for row in query_rows
            ]
        )
        distances = cdist(reference, query, metric="euclidean")
        n_reference, n_query = distances.shape
        maximum_pairs = min(n_reference, n_query)
        unmatched_cost = (maximum_pairs + 1) * (max_distance + 1.0)
        forbidden_cost = unmatched_cost * (n_reference + n_query + 1) * 4.0

        costs = np.full(
            (n_reference + n_query, n_reference + n_query),
            forbidden_cost,
            dtype=float,
        )
        costs[:n_reference, :n_query] = np.where(
            distances <= max_distance,
            distances,
            forbidden_cost,
        )
        costs[np.arange(n_reference), n_query + np.arange(n_reference)] = unmatched_cost
        costs[n_reference + np.arange(n_query), np.arange(n_query)] = unmatched_cost
        costs[n_reference:, n_query:] = 0.0

        row_indices, column_indices = linear_sum_assignment(costs)
        matches = [
            (
                int(reference_index),
                int(query_index),
                float(distances[reference_index, query_index]),
            )
            for reference_index, query_index in zip(
                row_indices, column_indices, strict=True
            )
            if reference_index < n_reference
            and query_index < n_query
            and distances[reference_index, query_index] <= max_distance
        ]
        return sorted(matches)


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
        nearest_neighbor_distance: Annotated[
            float, GUIMeta("Nearest neighbor distance")
        ]
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
        from scipy.spatial import KDTree

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
        for row_number, value in enumerate(result["spot_id"], start=1):
            positive_uint32_id(value, f"spot_id at row {row_number}")

        image = planar_array(
            iio.imread(arguments.image).astype(np.float32),
            "image",
        )
        if not np.all(np.isfinite(image)):
            raise ValueError("image must contain only finite intensities.")
        coordinates = np.asarray(
            [
                pixel_coordinate(row["y"], row["x"], image.shape)
                for _, row in result.iterrows()
            ],
            dtype=float,
        )
        continuous_coordinates = (
            coordinates[:, :2] if len(coordinates) else np.empty((0, 2))
        )
        pixel_coordinates = (
            coordinates[:, 2:].astype(int)
            if len(coordinates)
            else np.empty((0, 2), dtype=int)
        )

        nearest_distances = np.zeros(len(result), dtype=float)
        if len(result) > 1:
            nearest_distances = KDTree(continuous_coordinates).query(
                continuous_coordinates,
                k=2,
            )[0][:, 1]

        metrics = []
        radius = integral_value(getattr(arguments, "radius", 2), "radius", minimum=1)
        outer_radius = radius * 2
        spot_footprints = np.zeros(image.shape, dtype=bool)
        for y, x in pixel_coordinates:
            _draw_disk(spot_footprints, int(y), int(x), radius, True)

        for index, (_, row) in enumerate(result.iterrows(), start=1):
            row_dict = row.to_dict()
            y, x = (int(value) for value in pixel_coordinates[index - 1])
            y0 = max(0, y - outer_radius)
            y1 = min(image.shape[0], y + outer_radius + 1)
            x0 = max(0, x - outer_radius)
            x1 = min(image.shape[1], x + outer_radius + 1)
            yy, xx = np.ogrid[y0:y1, x0:x1]
            distance_squared = (yy - y) ** 2 + (xx - x) ** 2
            annulus = (distance_squared > radius**2) & (
                distance_squared <= outer_radius**2
            )
            annulus &= ~spot_footprints[y0:y1, x0:x1]
            samples = image[y0:y1, x0:x1][annulus]
            if not samples.size:
                raise ValueError(
                    f"Spot at ({y}, {x}) has no background pixels in its clipped "
                    f"radius-{radius}-to-{outer_radius} annulus."
                )
            background = float(np.median(samples))
            noise = float(np.std(samples))
            intensity = _row_float(row_dict, "intensity", float(image[y, x]))
            metrics.append(
                {
                    "local_background": background,
                    "snr": (intensity - background)
                    / max(noise, float(np.finfo(np.float32).eps)),
                    "nearest_neighbor_distance": nearest_distances[index - 1],
                }
            )
        result["local_background"] = [metric["local_background"] for metric in metrics]
        result["snr"] = [metric["snr"] for metric in metrics]
        result["nearest_neighbor_distance"] = [
            metric["nearest_neighbor_distance"] for metric in metrics
        ]
        result["spot_count"] = len(result)
        return result
