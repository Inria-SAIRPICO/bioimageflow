"""SAIRPICO hotspot tools that execute in the Python 3.9 hotspot environment."""

from pathlib import Path
import sys
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)

package_root = str(Path(__file__).resolve().parent.parent)
if package_root not in sys.path:
    sys.path.insert(0, package_root)

from bioimageflow_sairpico_tools._common import (  # noqa: E402
    IntensityImage,
    _ensure_output_parent,
    _hotspot_components,
    _run,
    hotspot_env,
)


class HotspotDetection(ProcessingTool):
    """Run hotspot detection."""

    display_name = "Hotspot Detection"
    documentation = "Detect hotspots in microscopy images using the hotspot command-line tool."
    category = Category.SPOT_DETECTION
    tags = ["sairpico", "hotspot", "detection"]
    environment = hotspot_env

    class Inputs(IOModel):
        input_image: IntensityImage
        patch_size: Annotated[int, GUIMeta("Patch size", "Patch radius.", min=1, step=1)] = 3
        neighborhood_size: Annotated[int, GUIMeta("Neighborhood size", "Neighborhood radius.", min=1, step=1)] = 5
        p_value: Annotated[float, GUIMeta("P-value", "False-alarm p-value.", min=0.0, max=1.0)] = 0.2

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Hotspot image", "Detected hotspots image."),
        ] = Template("{input_image.stem}_hotspot{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        _run([
            "hotSpotDetection",
            "-i", arguments.input_image,
            "-o", output_path,
            "-m", arguments.patch_size,
            "-n", arguments.neighborhood_size,
            "-pv", arguments.p_value,
        ])
        return self.Outputs(output_image=output_path)


class HotspotToSpots(ProcessingTool):
    """Convert thresholded hotspot images into spot coordinate tables."""

    display_name = "Hotspot To Spots"
    documentation = "Convert SAIRPICO hotspot image outputs to spot coordinate tables."
    category = Category.SPOT_DETECTION
    tags = ["sairpico", "hotspot", "spots"]
    environment = hotspot_env

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

        image = iio.imread(arguments.hotspot_image).astype(np.float32)
        if image.ndim != 2:
            raise ValueError("HotspotToSpots expects a 2D hotspot image.")
        components = _hotspot_components(image > float(arguments.threshold))
        rows = []
        for spot_id, component in enumerate(components, start=1):
            yy = np.asarray([yx[0] for yx in component], dtype=np.float32)
            xx = np.asarray([yx[1] for yx in component], dtype=np.float32)
            values = image[yy.astype(int), xx.astype(int)]
            rows.append(
                {
                    "spot_id": spot_id,
                    "y": float(yy.mean()),
                    "x": float(xx.mean()),
                    "intensity": float(values.max()),
                    "score": float(values.mean()),
                    "area": int(len(component)),
                    "label": spot_id,
                }
            )

        return [
            self.Outputs(
                spot_id=int(row["spot_id"]),
                y=float(row["y"]),
                x=float(row["x"]),
                intensity=float(row["intensity"]),
                score=float(row["score"]),
                area=int(row["area"]),
                label=int(row["label"]),
                spot_count=len(rows),
            )
            for row in rows
        ]
