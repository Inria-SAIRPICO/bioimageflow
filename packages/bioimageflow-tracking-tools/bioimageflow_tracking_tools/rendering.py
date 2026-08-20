"""Render object-track mappings back into label images."""

from pathlib import Path
from typing import Annotated, Any

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

from ._validation import integral_value, validate_label_image


class TracksToLabels(ProcessingTool):
    """Render a validated one-to-one object-track mapping as a label stack."""

    row_consumption = RowConsumption.COLLECTIVE
    display_name = "Tracks To Labels"
    documentation = "Render track IDs back into a label stack."
    category = Category.TRACKING
    tags = ["tracking", "labels", "render"]
    environment = GENERAL_ENV
    run_empty_batch = True
    empty_batch_anchor_inputs = ("label_image",)

    class Inputs(IOModel):
        track_id: Annotated[
            int, GUIMeta("Track ID", connectable=Connectable.BY_DEFAULT)
        ]
        frame: Annotated[int, GUIMeta("Frame", connectable=Connectable.BY_DEFAULT)]
        label: Annotated[int, GUIMeta("Label", connectable=Connectable.BY_DEFAULT)]
        label_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL}, layouts={Layout.PLANAR, Layout.PLANAR_TIME}
            ),
            GUIMeta("Source labels", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        output_label_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR_TIME},
                dtypes={"uint32"},
            ),
            GUIMeta("Track labels"),
        ] = Template("{label_image.stem}_tracks.tif")
        track_count: Annotated[int, GUIMeta("Track count")]

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

        track_arguments: list[Arguments] = []
        anchor_arguments: list[Arguments] = []
        track_positions: list[int] = []
        anchor_positions: list[int] = []
        for position, row in enumerate(arguments_list):
            present = [hasattr(row, field) for field in ("track_id", "frame", "label")]
            if any(present):
                if not all(present):
                    missing = next(
                        field
                        for field, exists in zip(
                            ("track_id", "frame", "label"), present, strict=True
                        )
                        if not exists
                    )
                    raise ValueError(
                        f"Track mapping row is missing required column {missing!r}."
                    )
                track_arguments.append(row)
                track_positions.append(position)
            else:
                anchor_arguments.append(row)
                anchor_positions.append(position)

        rows_by_source: dict[Path, list[tuple[int, Arguments]]] = {}
        output_by_source: dict[Path, Path] = {}
        source_by_output: dict[Path, Path] = {}
        for position, row in zip(track_positions, track_arguments, strict=True):
            source_path = Path(row.label_image)
            output_path = Path(row.output_label_image)
            previous_output = output_by_source.setdefault(source_path, output_path)
            if output_path != previous_output:
                raise ValueError(
                    "TracksToLabels rows for one label_image must reference the same output_label_image."
                )
            previous_source = source_by_output.setdefault(output_path, source_path)
            if source_path != previous_source:
                raise ValueError(
                    "TracksToLabels cannot write multiple source images to the same output_label_image."
                )
            rows_by_source.setdefault(source_path, []).append((position, row))

        rendered: list[list[Any]] = [[] for _ in arguments_list]
        for rows in rows_by_source.values():
            rendered[rows[0][0]] = self._render_tracks(
                [row for _, row in rows], iio=iio, np=np
            )
        rendered_sources = set(rows_by_source)
        rendered_outputs = set(source_by_output)
        for position, row in zip(anchor_positions, anchor_arguments, strict=True):
            source_path = Path(row.label_image)
            output_path = Path(row.output_label_image)
            if source_path in rendered_sources:
                if output_by_source[source_path] != output_path:
                    raise ValueError(
                        "TracksToLabels rows for one label_image must reference the same output_label_image."
                    )
                continue
            if output_path in rendered_outputs:
                raise ValueError(
                    "TracksToLabels cannot write multiple source images to the same output_label_image."
                )
            rendered[position] = self._render_empty(row, iio=iio, np=np)
            rendered_sources.add(source_path)
            rendered_outputs.add(output_path)
            output_by_source[source_path] = output_path
        return rendered

    def _render_tracks(
        self,
        track_arguments: list[Arguments],
        *,
        iio: Any,
        np: Any,
    ) -> list[Any]:
        first = track_arguments[0]
        source_path = Path(first.label_image)
        output_path = Path(first.output_label_image)

        source = iio.imread(source_path)
        validate_label_image(source, "TracksToLabels")
        if source.ndim == 2:
            source = source[np.newaxis, ...]

        uint32_max = int(np.iinfo(np.uint32).max)
        mappings: list[tuple[int, int, int]] = []
        for row in track_arguments:
            frame = integral_value(row.frame, "frame", minimum=0)
            label = integral_value(
                row.label,
                "label",
                minimum=1,
                maximum=uint32_max,
            )
            track_id = integral_value(
                row.track_id,
                "track_id",
                minimum=1,
                maximum=uint32_max,
            )
            if frame >= source.shape[0]:
                raise ValueError(
                    f"frame {frame} is outside the source label stack with {source.shape[0]} frame(s)."
                )
            mappings.append((frame, label, track_id))

        object_keys = [(frame, label) for frame, label, _ in mappings]
        if len(object_keys) != len(set(object_keys)):
            raise ValueError(
                "TracksToLabels received duplicate assignments for a source object."
            )
        track_frame_keys = [(track_id, frame) for frame, _, track_id in mappings]
        if len(track_frame_keys) != len(set(track_frame_keys)):
            raise ValueError(
                "TracksToLabels received multiple objects for one track and frame."
            )

        output_image = np.zeros(source.shape, dtype=np.uint32)
        for frame in sorted({mapping[0] for mapping in mappings}):
            frame_mappings = [mapping for mapping in mappings if mapping[0] == frame]
            labels = np.asarray(
                sorted(mapping[1] for mapping in frame_mappings), dtype=np.uint64
            )
            tracks_by_label = {label: track_id for _, label, track_id in frame_mappings}
            plane = source[frame]
            present_labels = set(int(value) for value in np.unique(plane))
            missing = [
                int(label) for label in labels if int(label) not in present_labels
            ]
            if missing:
                raise ValueError(
                    f"Source frame {frame} does not contain mapped label(s): {missing}."
                )
            mask = np.isin(plane, labels)
            sorted_tracks = np.asarray(
                [tracks_by_label[int(label)] for label in labels],
                dtype=np.uint32,
            )
            output_image[frame][mask] = sorted_tracks[
                np.searchsorted(labels, plane[mask].astype(np.uint64, copy=False))
            ]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output_path, output_image, photometric="minisblack")
        rendered_track_count = int(np.unique(output_image[output_image > 0]).size)
        return [
            self.Outputs(
                output_label_image=output_path,
                track_count=rendered_track_count,
            )
        ]

    def _render_empty(self, arguments: Arguments, *, iio: Any, np: Any) -> list[Any]:
        source = iio.imread(arguments.label_image)
        validate_label_image(source, "TracksToLabels")
        if source.ndim == 2:
            source = source[np.newaxis, ...]
        output_image = np.zeros(source.shape, dtype=np.uint32)
        output_path = Path(arguments.output_label_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output_path, output_image, photometric="minisblack")
        return [self.Outputs(output_label_image=output_path, track_count=0)]
