"""SAIRPICO hotspot tools that execute in the Python 3.9 hotspot environment."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)

from ._common import (
    IntensityImage,
    _ensure_output_parent,
    _require_finite,
    _require_integer,
    _run_with_staged_output,
    hotspot_env,
)


class HotspotDetection(ProcessingTool):
    """Run hotspot detection."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Hotspot Detection"
    documentation = (
        "Detect hotspots in microscopy images using the hotspot command-line tool."
    )
    category = Category.SPOT_DETECTION
    tags = ["sairpico", "hotspot", "detection"]
    environment = hotspot_env

    class Inputs(IOModel):
        input_image: IntensityImage
        patch_size: Annotated[
            int, GUIMeta("Patch size", "Patch radius.", min=1, step=1)
        ] = 3
        neighborhood_size: Annotated[
            int, GUIMeta("Neighborhood size", "Neighborhood radius.", min=1, step=1)
        ] = 5
        p_value: Annotated[
            float, GUIMeta("P-value", "False-alarm p-value.", min=0.0, max=1.0)
        ] = 0.2

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Hotspot image", "Detected hotspots image."),
        ] = Template("{input_image.stem}_hotspot.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        patch_size = _require_integer(arguments.patch_size, "patch_size", minimum=1)
        neighborhood_size = _require_integer(
            arguments.neighborhood_size,
            "neighborhood_size",
            minimum=1,
        )
        p_value = _require_finite(
            arguments.p_value, "p_value", minimum=0.0, maximum=1.0
        )
        output_path = _ensure_output_parent(arguments.output_image)
        _run_with_staged_output(
            [
                "hotSpotDetection",
                "-i",
                arguments.input_image,
                "-o",
                output_path,
                "-m",
                patch_size,
                "-n",
                neighborhood_size,
                "-pv",
                p_value,
            ],
            output_path,
        )
        return self.Outputs(output_image=output_path)


class HotspotToSpots(ProcessingTool):
    """Convert thresholded hotspot images into spot coordinate tables."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Hotspot To Spots"
    documentation = "Convert SAIRPICO hotspot image outputs to spot coordinate tables."
    category = Category.SPOT_DETECTION
    tags = ["sairpico", "hotspot", "spots"]
    environment = hotspot_env
    zero_row_scalar_outputs = {"spot_count": 0}

    class Inputs(IOModel):
        hotspot_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("Hotspot image", "Thresholded or scored hotspot image."),
        ]
        threshold: float = 0.0

    class Outputs(IOModel):
        spot_id: Annotated[int, GUIMeta("Spot ID")]
        y: Annotated[float, GUIMeta("Y")]
        x: Annotated[float, GUIMeta("X")]
        intensity: Annotated[float, GUIMeta("Intensity")]
        score: Annotated[float, GUIMeta("Score")]
        area: Annotated[int, GUIMeta("Area")]
        label: Annotated[int, GUIMeta("Label")]
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np
        from scipy import ndimage

        threshold = _require_finite(arguments.threshold, "threshold")
        image = np.asarray(iio.imread(arguments.hotspot_image))
        if image.ndim != 2:
            raise ValueError("HotspotToSpots expects a 2D hotspot image.")
        if not np.issubdtype(image.dtype, np.number) or np.issubdtype(
            image.dtype,
            np.complexfloating,
        ):
            raise ValueError("HotspotToSpots expects a real numeric image.")
        image = image.astype(np.float64, copy=False)
        if not np.all(np.isfinite(image)):
            raise ValueError("HotspotToSpots expects only finite image values.")

        foreground = image > threshold
        label_result: Any = ndimage.label(
            foreground,
            structure=np.ones((3, 3), dtype=np.uint8),
        )
        labels, spot_count = label_result
        if spot_count == 0:
            return []

        label_ids = np.arange(1, spot_count + 1)
        centroids = ndimage.center_of_mass(foreground, labels, label_ids)
        intensities = ndimage.maximum(image, labels, label_ids)
        scores = ndimage.mean(image, labels, label_ids)
        areas = ndimage.sum(foreground, labels, label_ids)

        return [
            self.Outputs(
                spot_id=spot_id,
                y=float(centroids[spot_id - 1][0]),
                x=float(centroids[spot_id - 1][1]),
                intensity=float(intensities[spot_id - 1]),
                score=float(scores[spot_id - 1]),
                area=int(areas[spot_id - 1]),
                label=spot_id,
                spot_count=spot_count,
            )
            for spot_id in range(1, spot_count + 1)
        ]
