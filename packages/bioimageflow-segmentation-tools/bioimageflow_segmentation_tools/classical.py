"""Lightweight classical segmentation tools."""

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

from ._arrays import (
    finite_float,
    integer_parameter,
    object_count,
    relabel_sequential,
    validate_image,
    validate_labels,
    write_labels,
)


def _label_skimage(mask: Any) -> Any:
    import numpy as np
    from skimage import measure

    return np.asarray(measure.label(mask, connectivity=1)).astype("uint32", copy=False)


def _distance_watershed(mask: Any, min_distance: int) -> tuple[Any, int]:
    import numpy as np
    from scipy import ndimage as ndi
    from skimage import measure
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed

    foreground = np.asarray(mask, dtype=bool)
    if not foreground.any():
        return np.zeros(foreground.shape, dtype=np.uint32), 0

    marker_distance = integer_parameter(min_distance, name="min_distance", minimum=1)
    distance = np.asarray(ndi.distance_transform_edt(foreground), dtype=np.float64)
    peak_coordinates = peak_local_max(
        distance,
        labels=foreground,
        min_distance=marker_distance,
        exclude_border=False,
    )
    markers = np.zeros(foreground.shape, dtype=np.uint32)
    if peak_coordinates.size:
        marker_indices = tuple(peak_coordinates[:, axis] for axis in range(foreground.ndim))
        markers[marker_indices] = np.arange(
            1, len(peak_coordinates) + 1, dtype=np.uint32
        )
    else:
        markers = np.asarray(measure.label(foreground, connectivity=1)).astype(
            "uint32", copy=False
        )

    labels = np.asarray(watershed(-distance, markers, mask=foreground)).astype(
        "uint32", copy=False
    )
    return labels, object_count(labels)


class ThresholdSegment(ProcessingTool):
    """Threshold an image and label connected foreground components."""

    row_consumption = RowConsumption.MAPPED
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
                description="Pixels strictly greater than this value become foreground.",
                group="general",
            ),
        ] = 0.5
        above: Annotated[
            bool,
            GUIMeta(
                display_name="Use values above threshold",
                description="When false, pixels at or below the threshold become foreground.",
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
        ] = Template("{input_image.stem}_threshold_labels.tif")
        object_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of connected foreground objects.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        threshold = finite_float(arguments.threshold, name="threshold")
        if arguments.above:
            foreground = image > threshold
        else:
            foreground = image <= threshold
        labels = _label_skimage(foreground)

        labels_path = Path(arguments.labels)
        write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=object_count(labels))


class OtsuThresholdSegment(ProcessingTool):
    """Threshold an image with scikit-image's global Otsu threshold."""

    row_consumption = RowConsumption.MAPPED
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
        ] = Template("{input_image.stem}_otsu_labels.tif")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]
        threshold: Annotated[float, GUIMeta(display_name="Otsu threshold")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        from skimage.filters import threshold_otsu

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        threshold = float(threshold_otsu(image))
        foreground = image > threshold if arguments.above else image <= threshold
        labels = _label_skimage(foreground)
        labels_path = Path(arguments.labels)
        write_labels(labels_path, labels)
        return self.Outputs(
            labels=labels_path,
            object_count=object_count(labels),
            threshold=threshold,
        )


class LocalThresholdSegment(ProcessingTool):
    """Threshold an image with scikit-image's Sauvola local threshold."""

    row_consumption = RowConsumption.MAPPED
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
        ] = Template("{input_image.stem}_local_threshold_labels.tif")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        from skimage.filters import threshold_sauvola

        block_size = integer_parameter(arguments.block_size, name="block_size", minimum=3)
        if block_size < 3 or block_size % 2 == 0:
            raise ValueError("block_size must be an odd integer greater than or equal to 3.")
        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        k = finite_float(arguments.k, name="k")
        offset = finite_float(arguments.offset, name="offset")
        threshold = threshold_sauvola(
            image,
            window_size=block_size,
            k=k,
        )
        threshold = threshold - offset
        foreground = image > threshold if arguments.above else image <= threshold
        labels = _label_skimage(foreground)
        labels_path = Path(arguments.labels)
        write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=object_count(labels))


class WatershedSegment(ProcessingTool):
    """Split foreground regions using marker-controlled watershed semantics."""

    row_consumption = RowConsumption.MAPPED
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
                description="Pixels strictly greater than this value are segmented.",
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
        ] = Template("{input_image.stem}_watershed_labels.tif")
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

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        threshold = finite_float(arguments.threshold, name="threshold")
        foreground = image > threshold
        markers_value = arguments.markers_image
        if markers_value is None or str(markers_value) == "":
            markers = np.asarray(measure.label(foreground, connectivity=1)).astype(
                "uint32", copy=False
            )
        else:
            markers = validate_labels(
                iio.imread(str(markers_value)),
                name="markers_image",
                expected_shape=foreground.shape,
            )

        labels = watershed(
            -image.astype("float32", copy=False), markers, mask=foreground
        )
        labels = labels.astype("uint32", copy=False)
        count = object_count(labels)

        labels_path = Path(arguments.labels)
        write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=count)


class DistanceWatershedSegment(ProcessingTool):
    """Split foreground using marker-free distance-transform watershed."""

    row_consumption = RowConsumption.MAPPED
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
                description="Pixels strictly greater than this value are segmented.",
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
        ] = Template("{input_image.stem}_distance_watershed_labels.tif")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        threshold = finite_float(arguments.threshold, name="threshold")
        labels, object_count = _distance_watershed(
            image > threshold,
            min_distance=arguments.min_distance,
        )
        labels_path = Path(arguments.labels)
        write_labels(labels_path, labels)
        return self.Outputs(labels=labels_path, object_count=object_count)


class SplitTouchingObjects(ProcessingTool):
    """Split clumped labels with distance-transform watershed semantics."""

    row_consumption = RowConsumption.MAPPED
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
        ] = Template("{labels.stem}_split.tif")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        from skimage.measure import regionprops

        source = validate_labels(iio.imread(str(arguments.labels)))
        min_distance = integer_parameter(
            arguments.min_distance,
            name="min_distance",
            minimum=1,
        )
        output = np.zeros(source.shape, dtype=np.uint32)
        next_label = 1
        for region in regionprops(source):
            source_view = source[region.slice]
            split, _ = _distance_watershed(
                source_view == region.label,
                min_distance=min_distance,
            )
            split_count = object_count(split)
            output_view = output[region.slice]
            foreground = split > 0
            output_view[foreground] = split[foreground] + (next_label - 1)
            next_label += split_count

        output_path = Path(arguments.output_labels)
        write_labels(output_path, output)
        return self.Outputs(output_labels=output_path, object_count=next_label - 1)


class FilterLabels(ProcessingTool):
    """Filter labels by area, border contact, intensity, and shape constraints."""

    row_consumption = RowConsumption.MAPPED
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
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
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
        ] = Template("{labels.stem}_filtered.tif")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np
        from skimage.measure import regionprops

        source = validate_labels(iio.imread(str(arguments.labels)))
        min_area = integer_parameter(arguments.min_area, name="min_area")
        max_area = integer_parameter(arguments.max_area, name="max_area")
        if max_area and max_area < min_area:
            raise ValueError("max_area must be zero or greater than or equal to min_area.")
        min_mean_intensity = finite_float(
            arguments.min_mean_intensity,
            name="min_mean_intensity",
        )
        min_solidity = finite_float(arguments.min_solidity, name="min_solidity")
        max_eccentricity = finite_float(
            arguments.max_eccentricity,
            name="max_eccentricity",
        )
        if not 0.0 <= min_solidity <= 1.0:
            raise ValueError("min_solidity must be between 0 and 1.")
        if not 0.0 <= max_eccentricity <= 1.0:
            raise ValueError("max_eccentricity must be between 0 and 1.")
        if source.ndim != 2 and (min_solidity != 0.0 or max_eccentricity != 1.0):
            raise ValueError(
                "min_solidity and max_eccentricity are only defined for planar labels; "
                "use their defaults for volumetric labels."
            )
        intensity = None
        intensity_path = arguments.intensity_image
        if intensity_path is not None and str(intensity_path) != "":
            intensity = validate_image(
                iio.imread(str(intensity_path)),
                name="intensity_image",
                dimensions=(source.ndim,),
            )
            if intensity.shape != source.shape:
                raise ValueError(
                    f"intensity_image must have shape {source.shape}; got {intensity.shape}."
                )

        output = np.zeros(source.shape, dtype=np.uint32)
        next_label = 1
        for region in regionprops(source, intensity_image=intensity):
            if region.area < min_area:
                continue
            if max_area and region.area > max_area:
                continue
            if arguments.remove_border_touching and _touches_border(
                region.bbox,
                source.shape,
            ):
                continue
            if (
                intensity is not None
                and region.mean_intensity < min_mean_intensity
            ):
                continue
            if source.ndim == 2 and region.solidity < min_solidity:
                continue
            if source.ndim == 2 and region.eccentricity > max_eccentricity:
                continue
            output_view = output[region.slice]
            output_view[region.image] = next_label
            next_label += 1

        output_path = Path(arguments.output_labels)
        write_labels(output_path, output)
        return self.Outputs(output_labels=output_path, object_count=next_label - 1)


class PostprocessLabels(ProcessingTool):
    """Filter and relabel a label image."""

    row_consumption = RowConsumption.MAPPED
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
        ] = Template("{labels.stem}_postprocessed.tif")
        object_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of labels retained.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        labels = validate_labels(iio.imread(str(arguments.labels)))
        min_size = integer_parameter(arguments.min_size, name="min_size")
        relabeled, count = relabel_sequential(labels, min_size=min_size)

        output_path = Path(arguments.output_labels)
        write_labels(output_path, relabeled)
        return self.Outputs(output_labels=output_path, object_count=count)


def _touches_border(bbox: tuple[int, ...], shape: tuple[int, ...]) -> bool:
    ndim = len(shape)
    starts = bbox[:ndim]
    stops = bbox[ndim:]
    return any(start == 0 for start in starts) or any(
        stop == size for stop, size in zip(stops, shape, strict=True)
    )
