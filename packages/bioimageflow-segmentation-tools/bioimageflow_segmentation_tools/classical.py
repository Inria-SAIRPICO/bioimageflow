"""Lightweight classical segmentation tools."""

from collections import deque
from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)

classical_segmentation_env = EnvironmentSpec(
    name="segmentation-classical",
    dependencies={
        "python": "3.12",
        "pip": ["imageio", "numpy", "scikit-image", "tifffile"],
    },
)


def _neighbors(index: tuple[int, ...], shape: tuple[int, ...]) -> list[tuple[int, ...]]:
    neighbors = []
    for axis, coordinate in enumerate(index):
        if coordinate > 0:
            lower = list(index)
            lower[axis] -= 1
            neighbors.append(tuple(lower))
        if coordinate + 1 < shape[axis]:
            upper = list(index)
            upper[axis] += 1
            neighbors.append(tuple(upper))
    return neighbors


def _label_connected(mask: Any) -> Any:
    import numpy as np

    foreground = np.asarray(mask, dtype=bool)
    labels = np.zeros(foreground.shape, dtype=np.uint32)
    next_label = 1

    for start in np.argwhere(foreground):
        start_index = tuple(int(v) for v in start)
        if labels[start_index] != 0:
            continue

        queue: deque[tuple[int, ...]] = deque([start_index])
        labels[start_index] = next_label
        while queue:
            current = queue.popleft()
            for neighbor in _neighbors(current, foreground.shape):
                if foreground[neighbor] and labels[neighbor] == 0:
                    labels[neighbor] = next_label
                    queue.append(neighbor)

        next_label += 1

    return labels


def _relabel_sequential(labels: Any, min_size: int = 0) -> tuple[Any, int]:
    import numpy as np

    source = np.asarray(labels)
    output = np.zeros(source.shape, dtype=np.uint32)
    next_label = 1
    for label in np.unique(source):
        label_int = int(label)
        if label_int == 0:
            continue
        mask = source == label_int
        if int(mask.sum()) < min_size:
            continue
        output[mask] = next_label
        next_label += 1
    return output, next_label - 1


class ThresholdSegment(ProcessingTool):
    """Threshold an image and label connected foreground components."""

    display_name = "Threshold Segment"
    documentation = (
        "Create a binary foreground mask from an intensity threshold and label "
        "each connected object."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "threshold", "classical"]
    environment = classical_segmentation_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY, Semantic.PROBABILITY},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Input image",
                description="Intensity or probability image to threshold.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        threshold: Annotated[
            float,
            GUIMeta(
                display_name="Threshold",
                description="Pixels greater than or equal to this value become foreground.",
                group="general",
            ),
        ] = 0.5
        above: Annotated[
            bool,
            GUIMeta(
                display_name="Use values above threshold",
                description="When false, pixels less than or equal to the threshold become foreground.",
                group="general",
            ),
        ] = True

    class Outputs(IOModel):
        labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Label image",
                description="Connected foreground components as integer labels.",
            ),
        ] = Template("{input_image.stem}_threshold_labels{ext}")
        object_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of connected foreground objects.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = iio.imread(str(arguments.input_image))
        if arguments.above:
            foreground = image >= arguments.threshold
        else:
            foreground = image <= arguments.threshold
        labels = _label_connected(foreground)
        object_count = int(labels.max())

        labels_path = Path(arguments.labels)
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(labels_path), labels)
        return self.Outputs(labels=labels_path, object_count=object_count)


class WatershedSegment(ProcessingTool):
    """Split foreground regions using marker-controlled watershed semantics."""

    display_name = "Watershed Segment"
    documentation = (
        "Segment thresholded foreground regions. When marker labels are supplied, "
        "foreground pixels are assigned to the nearest marker by graph distance."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "watershed", "classical"]
    environment = classical_segmentation_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY, Semantic.PROBABILITY, Semantic.BINARY},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Input image",
                description="Image used to define the foreground region.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        markers_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Markers image",
                description="Optional marker label image used to split foreground regions.",
                connectable=Connectable.NOT_BY_DEFAULT,
                group="general",
            ),
        ] = ""
        threshold: Annotated[
            float,
            GUIMeta(
                display_name="Foreground threshold",
                description="Pixels greater than or equal to this value are segmented.",
                group="general",
            ),
        ] = 0.5

    class Outputs(IOModel):
        labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Label image",
                description="Watershed segmentation label image.",
            ),
        ] = Template("{input_image.stem}_watershed_labels{ext}")
        object_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of watershed objects.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np
        from skimage import measure
        from skimage.segmentation import watershed

        image = iio.imread(str(arguments.input_image))
        foreground = image >= arguments.threshold
        markers_value = getattr(arguments, "markers_image", "")
        if markers_value is None or str(markers_value) == "":
            markers = measure.label(foreground).astype("uint32", copy=False)
        else:
            markers = iio.imread(str(markers_value))
            if markers.shape != foreground.shape:
                raise ValueError(
                    "markers_image must have the same shape as input_image "
                    f"({markers.shape} != {foreground.shape})"
                )

        labels = watershed(
            -image.astype("float32", copy=False), markers, mask=foreground
        )
        labels = labels.astype("uint32", copy=False)
        object_count = int(np.count_nonzero(np.unique(labels)))

        labels_path = Path(arguments.labels)
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(labels_path), labels)
        return self.Outputs(labels=labels_path, object_count=object_count)


class PostprocessLabels(ProcessingTool):
    """Filter and relabel a label image."""

    display_name = "Postprocess Labels"
    documentation = (
        "Remove labels below a minimum size and relabel remaining labels sequentially."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "labels", "postprocessing", "classical"]
    environment = classical_segmentation_env

    class Inputs(IOModel):
        labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Label image",
                description="Input label image to postprocess.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        min_size: Annotated[
            int,
            GUIMeta(
                display_name="Minimum label size",
                description="Labels with fewer pixels/voxels than this value are removed.",
                min=0,
                step=1,
                group="general",
            ),
        ] = 0

    class Outputs(IOModel):
        output_labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Output labels",
                description="Filtered label image with sequential label IDs.",
            ),
        ] = Template("{labels.stem}_postprocessed{ext}")
        object_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of labels retained.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        labels = iio.imread(str(arguments.labels))
        relabeled, object_count = _relabel_sequential(labels, arguments.min_size)

        output_path = Path(arguments.output_labels)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(output_path), relabeled)
        return self.Outputs(output_labels=output_path, object_count=object_count)
