"""Lightweight classical segmentation tools."""

from collections import deque
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
    Semantic,
    Template,
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


def _write_labels(path: Path, labels: Any) -> None:
    import imageio.v3 as iio
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.asarray(labels)
    kwargs = {"photometric": "minisblack"} if array.ndim >= 3 else {}
    iio.imwrite(str(path), array, **kwargs)


def _label_skimage(mask: Any) -> Any:
    import numpy as np
    from skimage import measure

    return np.asarray(measure.label(mask)).astype("uint32", copy=False)


def _distance_transform(mask: Any) -> Any:
    try:
        from scipy import ndimage as ndi

        return ndi.distance_transform_edt(mask)
    except ImportError:
        from skimage.morphology import medial_axis

        _, distance = medial_axis(mask, return_distance=True)
        return distance


def _distance_watershed(mask: Any, min_distance: int) -> tuple[Any, int]:
    import numpy as np
    from skimage import measure
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed

    foreground = np.asarray(mask, dtype=bool)
    if not foreground.any():
        return np.zeros(foreground.shape, dtype=np.uint32), 0

    distance = _distance_transform(foreground)
    peak_coordinates = peak_local_max(
        distance,
        labels=foreground,
        min_distance=max(1, int(min_distance)),
        exclude_border=False,
    )
    markers = np.zeros(foreground.shape, dtype=np.uint32)
    for marker_label, coordinate in enumerate(peak_coordinates, start=1):
        markers[tuple(int(value) for value in coordinate)] = marker_label
    if markers.max() == 0:
        markers = np.asarray(measure.label(foreground)).astype("uint32", copy=False)

    labels = np.asarray(watershed(-distance, markers, mask=foreground)).astype(
        "uint32", copy=False
    )
    return labels, int(labels.max())


class ThresholdSegment(ProcessingTool):
    """Threshold an image and label connected foreground components."""

    display_name = "Threshold Segment"
    documentation = (
        "Create a binary foreground mask from an intensity threshold and label "
        "each connected object."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "threshold", "classical"]
    environment = GENERAL_ENV

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
        _write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=object_count)


class OtsuThresholdSegment(ProcessingTool):
    """Threshold an image with scikit-image's global Otsu threshold."""

    display_name = "Otsu Threshold Segment"
    documentation = "Compute a global Otsu threshold and label foreground components."
    category = Category.SEGMENTATION
    tags = ["segmentation", "threshold", "otsu", "classical"]
    environment = GENERAL_ENV

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
        above: Annotated[
            bool,
            GUIMeta(
                display_name="Use values above threshold",
                description="When false, pixels below or equal to Otsu become foreground.",
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
        ] = Template("{input_image.stem}_otsu_labels{ext}")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]
        threshold: Annotated[float, GUIMeta(display_name="Otsu threshold")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        from skimage.filters import threshold_otsu

        image = iio.imread(str(arguments.input_image))
        threshold = float(threshold_otsu(image))
        foreground = image > threshold if getattr(arguments, "above", True) else image <= threshold
        labels = _label_skimage(foreground)
        labels_path = Path(arguments.labels)
        _write_labels(labels_path, labels)
        return self.Outputs(
            labels=labels_path,
            object_count=int(labels.max()),
            threshold=threshold,
        )


class LocalThresholdSegment(ProcessingTool):
    """Threshold an image with scikit-image's Sauvola local threshold."""

    display_name = "Local Threshold Segment"
    documentation = "Compute a Sauvola adaptive threshold and label foreground."
    category = Category.SEGMENTATION
    tags = ["segmentation", "threshold", "sauvola", "local", "classical"]
    environment = GENERAL_ENV

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
        block_size: Annotated[
            int,
            GUIMeta(
                display_name="Block size",
                description="Odd local window size for Sauvola thresholding.",
                min=3,
                step=2,
                group="general",
            ),
        ] = 15
        k: Annotated[
            float,
            GUIMeta(
                display_name="Sauvola k",
                description="Sauvola sensitivity parameter.",
                group="general",
            ),
        ] = 0.2
        offset: Annotated[
            float,
            GUIMeta(
                display_name="Threshold offset",
                description="Value subtracted from the local threshold.",
                group="general",
            ),
        ] = 0.0
        above: Annotated[
            bool,
            GUIMeta(
                display_name="Use values above threshold",
                description="When false, pixels below or equal to threshold become foreground.",
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
        ] = Template("{input_image.stem}_local_threshold_labels{ext}")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        from skimage.filters import threshold_sauvola

        block_size = int(getattr(arguments, "block_size", 15))
        if block_size < 3 or block_size % 2 == 0:
            raise ValueError("block_size must be an odd integer greater than or equal to 3.")
        image = iio.imread(str(arguments.input_image))
        threshold = threshold_sauvola(
            image,
            window_size=block_size,
            k=getattr(arguments, "k", 0.2),
        )
        threshold = threshold - float(getattr(arguments, "offset", 0.0))
        foreground = image > threshold if getattr(arguments, "above", True) else image <= threshold
        labels = _label_skimage(foreground)
        labels_path = Path(arguments.labels)
        _write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=int(labels.max()))


class WatershedSegment(ProcessingTool):
    """Split foreground regions using marker-controlled watershed semantics."""

    display_name = "Watershed Segment"
    documentation = (
        "Segment thresholded foreground regions. When marker labels are supplied, "
        "foreground pixels are assigned to the nearest marker by graph distance."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "watershed", "classical"]
    environment = GENERAL_ENV

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
            Path | str | None,
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
            markers = np.asarray(measure.label(foreground)).astype(
                "uint32", copy=False
            )
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
        _write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=object_count)


class DistanceWatershedSegment(ProcessingTool):
    """Split foreground using marker-free distance-transform watershed."""

    display_name = "Distance Watershed Segment"
    documentation = "Use distance-transform peaks as watershed markers for foreground."
    category = Category.SEGMENTATION
    tags = ["segmentation", "watershed", "distance", "classical"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY, Semantic.PROBABILITY, Semantic.BINARY},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Input image",
                description="Image thresholded to define watershed foreground.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        threshold: Annotated[
            float,
            GUIMeta(
                display_name="Foreground threshold",
                description="Pixels greater than or equal to this value are segmented.",
                group="general",
            ),
        ] = 0.5
        min_distance: Annotated[
            int,
            GUIMeta(
                display_name="Minimum marker distance",
                description="Minimum distance between local distance peaks.",
                min=1,
                step=1,
                group="general",
            ),
        ] = 5

    class Outputs(IOModel):
        labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Label image",
                description="Distance watershed label image.",
            ),
        ] = Template("{input_image.stem}_distance_watershed_labels{ext}")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = iio.imread(str(arguments.input_image))
        labels, object_count = _distance_watershed(
            image >= getattr(arguments, "threshold", 0.5),
            min_distance=getattr(arguments, "min_distance", 5),
        )
        labels_path = Path(arguments.labels)
        _write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=object_count)


class SplitTouchingObjects(ProcessingTool):
    """Split clumped labels with distance-transform watershed semantics."""

    display_name = "Split Touching Objects"
    documentation = "Split touching foreground inside each label image mask."
    category = Category.SEGMENTATION
    tags = ["segmentation", "labels", "watershed", "postprocessing", "classical"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Label image",
                description="Input clumped label image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        min_distance: Annotated[
            int,
            GUIMeta(
                display_name="Minimum marker distance",
                description="Minimum distance between local distance peaks.",
                min=1,
                step=1,
                group="general",
            ),
        ] = 5

    class Outputs(IOModel):
        output_labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Output labels",
                description="Split label image with sequential label IDs.",
            ),
        ] = Template("{labels.stem}_split{ext}")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        source = iio.imread(str(arguments.labels))
        output = np.zeros(source.shape, dtype=np.uint32)
        next_label = 1
        for label in np.unique(source):
            label_int = int(label)
            if label_int == 0:
                continue
            split, _ = _distance_watershed(
                source == label_int,
                min_distance=getattr(arguments, "min_distance", 5),
            )
            for split_label in np.unique(split):
                if int(split_label) == 0:
                    continue
                output[split == split_label] = next_label
                next_label += 1

        output_path = Path(arguments.output_labels)
        _write_labels(output_path, output)
        return self.Outputs(output_labels=output_path, object_count=next_label - 1)


class FilterLabels(ProcessingTool):
    """Filter labels by area, border contact, intensity, and shape constraints."""

    display_name = "Filter Labels"
    documentation = "Remove labels by area, border contact, intensity, or shape."
    category = Category.SEGMENTATION
    tags = ["segmentation", "labels", "filter", "postprocessing", "classical"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        labels: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Label image",
                description="Input label image to filter.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        min_area: Annotated[int, GUIMeta(display_name="Minimum area", min=0)] = 0
        max_area: Annotated[int, GUIMeta(display_name="Maximum area", min=0)] = 0
        remove_border_touching: Annotated[
            bool,
            GUIMeta(display_name="Remove border-touching labels"),
        ] = False
        intensity_image: Annotated[
            Path | str | None,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Intensity image",
                description="Optional image used for mean-intensity filtering.",
                connectable=Connectable.NOT_BY_DEFAULT,
            ),
        ] = ""
        min_mean_intensity: Annotated[
            float,
            GUIMeta(display_name="Minimum mean intensity"),
        ] = 0.0
        min_solidity: Annotated[float, GUIMeta(display_name="Minimum solidity")] = 0.0
        max_eccentricity: Annotated[
            float,
            GUIMeta(display_name="Maximum eccentricity"),
        ] = 1.0

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
        ] = Template("{labels.stem}_filtered{ext}")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np
        from skimage.measure import regionprops

        source = iio.imread(str(arguments.labels))
        intensity = None
        intensity_path = getattr(arguments, "intensity_image", "")
        if intensity_path is not None and str(intensity_path) != "":
            intensity = iio.imread(str(intensity_path))
            if intensity.shape != source.shape:
                raise ValueError("intensity_image must have the same shape as labels.")

        output = np.zeros(source.shape, dtype=np.uint32)
        next_label = 1
        for region in regionprops(source, intensity_image=intensity):
            if region.area < getattr(arguments, "min_area", 0):
                continue
            max_area = getattr(arguments, "max_area", 0)
            if max_area and region.area > max_area:
                continue
            if getattr(arguments, "remove_border_touching", False) and _touches_border(
                region.bbox,
                source.shape,
            ):
                continue
            if (
                intensity is not None
                and region.mean_intensity < getattr(arguments, "min_mean_intensity", 0.0)
            ):
                continue
            if source.ndim == 2 and getattr(region, "solidity", 1.0) < getattr(
                arguments,
                "min_solidity",
                0.0,
            ):
                continue
            if source.ndim == 2 and getattr(region, "eccentricity", 0.0) > getattr(
                arguments,
                "max_eccentricity",
                1.0,
            ):
                continue
            output[source == region.label] = next_label
            next_label += 1

        output_path = Path(arguments.output_labels)
        _write_labels(output_path, output)
        return self.Outputs(output_labels=output_path, object_count=next_label - 1)


class PostprocessLabels(ProcessingTool):
    """Filter and relabel a label image."""

    display_name = "Postprocess Labels"
    documentation = (
        "Remove labels below a minimum size and relabel remaining labels sequentially."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "labels", "postprocessing", "classical"]
    environment = GENERAL_ENV

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
        _write_labels(output_path, relabeled)
        return self.Outputs(output_labels=output_path, object_count=object_count)


def _touches_border(bbox: tuple[int, ...], shape: tuple[int, ...]) -> bool:
    ndim = len(shape)
    starts = bbox[:ndim]
    stops = bbox[ndim:]
    return any(start == 0 for start in starts) or any(
        stop == size for stop, size in zip(stops, shape, strict=True)
    )
