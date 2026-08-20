"""Worker import contracts for first-party tool packages."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_worker_safe_tool_modules_do_not_import_orchestrator_package() -> None:
    code = textwrap.dedent(
        """
        import importlib
        import sys


        class BlockBioImageFlowImporter:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "bioimageflow" or fullname.startswith("bioimageflow."):
                    raise ModuleNotFoundError(
                        "orchestrator package must not be imported in worker-safe modules"
                    )
                return None


        sys.meta_path.insert(0, BlockBioImageFlowImporter())

        modules = [
            "bioimageflow_common_tools",
            "bioimageflow_io_tools",
            "bioimageflow_io_tools.bioio_convert",
            "bioimageflow_io_tools.metadata",
            "bioimageflow_io_tools.selection",
            "bioimageflow_io_tools.writers",
            "bioimageflow_measurement_tools",
            "bioimageflow_measurement_tools.processing_tools",
            "bioimageflow_restoration_tools",
            "bioimageflow_restoration_tools.baselines",
            "bioimageflow_restoration_tools.restore",
            "bioimageflow_sairpico_tools",
            "bioimageflow_sairpico_tools.cimgdenoising",
            "bioimageflow_sairpico_tools.hotspot",
            "bioimageflow_sairpico_tools.simglib",
            "bioimageflow_segmentation_tools",
            "bioimageflow_segmentation_tools.classical",
            "bioimageflow_spot_tools",
            "bioimageflow_spot_tools.atlas",
            "bioimageflow_spot_tools.assignment",
            "bioimageflow_spot_tools.detection",
            "bioimageflow_tracking_tools",
            "bioimageflow_tracking_tools.labels",
            "bioimageflow_tracking_tools.rendering",
        ]

        for module_name in modules:
            importlib.import_module(module_name)

        root_processing_exports = {
            "bioimageflow_common_tools": [
                "ConnectedComponents",
                "LabelOverlaps",
                "Mosaic",
            ],
            "bioimageflow_io_tools": [
                "BioIOConvertImage",
                "ConvertImageFormat",
                "ConvertToOmeTiff",
                "ConvertToOmeZarr",
                "ReadImageMetadata",
                "SelectChannel",
                "SelectDimensions",
                "SelectScene",
                "SelectTimepoint",
                "SelectZRange",
                "ValidateImageLayout",
            ],
            "bioimageflow_measurement_tools": [
                "CountLabels",
                "DiceIoU",
                "IntensityProperties",
                "LabelBenchmark",
                "ObjectMatchingMetrics",
                "RegionProperties",
                "ShapeProperties",
            ],
            "bioimageflow_restoration_tools": [
                "BackgroundSubtract",
                "BilateralDenoise",
                "GaussianDenoise",
                "MedianDenoise",
                "RichardsonLucyRestoration",
                "TotalVariationDenoise",
                "UnsharpMask",
            ],
            "bioimageflow_sairpico_tools": [
                "CImgDenoising",
                "GaussianPSF",
                "GibsonLanniPSF",
                "HotspotDetection",
                "HotspotToSpots",
                "MedianDenoising",
                "RichardsonLucyDeconvolution",
                "SpitfireDeconvolution",
                "WienerDeconvolution",
            ],
            "bioimageflow_segmentation_tools": [
                "Cellpose3",
                "DistanceWatershedSegment",
                "FilterLabels",
                "LocalThresholdSegment",
                "OtsuThresholdSegment",
                "PostprocessLabels",
                "SplitTouchingObjects",
                "StarDistSegmenter",
                "ThresholdSegment",
                "WatershedSegment",
            ],
            "bioimageflow_spot_tools": [
                "AssignSpotsToLabels",
                "AtlasSpotDetection",
                "DetectSpots",
                "MaskToLabels",
                "RenderSpots",
                "SpotsToLabels",
            ],
            "bioimageflow_tracking_tools": [
                "LabelsToObjects",
                "TracksToLabels",
            ],
        }

        for package_name, export_names in root_processing_exports.items():
            package = importlib.import_module(package_name)
            for export_name in export_names:
                getattr(package, export_name)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
