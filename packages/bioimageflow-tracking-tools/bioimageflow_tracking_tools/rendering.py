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
    collective_reference_inputs = ("label_image",)

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
        source_label_image: Annotated[Path, GUIMeta("Source labels")]
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

        references = [Arguments(**reference.arguments) for reference in getattr(context, "reference_rows", ())]
        batch = getattr(context, "batch_arguments", None)
        if batch is not None and hasattr(batch, "label_image"):
            references.append(batch)
        rows_by_source: dict[Path, list[Arguments]] = {}
        output_by_source: dict[Path, Path] = {}
        source_by_output: dict[Path, Path] = {}
        reference_by_source: dict[Path, Arguments] = {}
        for row in [*arguments_list, *references]:
            source = Path(row.label_image)
            output = Path(row.output_label_image)
            previous_output = output_by_source.setdefault(source, output)
            if output != previous_output:
                raise ValueError("TracksToLabels rows for one label_image must reference the same output_label_image.")
            previous_source = source_by_output.setdefault(output, source)
            if source != previous_source:
                raise ValueError("TracksToLabels cannot write multiple source images to the same output_label_image.")
            reference_by_source.setdefault(source, row)
        for row in arguments_list:
            for field in ("track_id", "frame", "label"):
                if not hasattr(row, field):
                    raise ValueError(f"Track mapping row is missing required column {field!r}.")
            rows_by_source.setdefault(Path(row.label_image), []).append(row)
        rendered: list[Any] = []
        for source, reference in reference_by_source.items():
            rows = rows_by_source.get(source, [])
            rendered.extend(self._render_tracks(rows, iio=iio, np=np) if rows else self._render_empty(reference, iio=iio, np=np))
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
                source_label_image=source_path,
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
        return [self.Outputs(source_label_image=Path(arguments.label_image), output_label_image=output_path, track_count=0)]
