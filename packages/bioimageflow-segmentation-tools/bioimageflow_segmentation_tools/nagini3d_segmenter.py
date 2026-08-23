"""NAGINI-3D volumetric segmentation adapter."""

from pathlib import Path
from typing import Annotated, Any, Literal

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
    RowConsumption,
    Semantic,
    Template,
)

from ._arrays import object_count, validate_image, validate_labels, write_labels


nagini3d_env = EnvironmentSpec(
    name="segmentation-nagini3d",
    dependencies={
        "python": "3.10",
        "pip": [
            "nagini3D==0.2.3",
            "numpy==1.26.4",
            "PyYAML==6.0.2",
            "tifffile==2024.2.12",
            "torch==2.7.1",
        ],
    },
)


class Nagini3DSegment(ProcessingTool):
    """Segment a volume and retain NAGINI's parametric surface representation."""

    row_consumption = RowConsumption.MAPPED
    display_name = "NAGINI-3D"
    documentation = (
        "Segment spherical-topology objects in a 3D volume with NAGINI-3D and "
        "write labels, probabilities, parametric surfaces, and curvature data."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "nagini3d", "3d", "surfaces", "deep learning"]
    environment = nagini3d_env

    class Inputs(IOModel):
        input_volume: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.VOLUMETRIC}),
            GUIMeta(
                display_name="Input volume",
                description="Single-channel ZYX intensity TIFF volume.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        model_bundle: Annotated[
            Path,
            GUIMeta(
                display_name="Model bundle",
                description="Directory containing config.yaml, thresholds.yaml, and weights.",
                connectable=Connectable.NEVER,
            ),
        ]
        weights_filename: Annotated[str, GUIMeta(display_name="Weights file")] = "best.pkl"
        probability_threshold: Annotated[
            float | None,
            GUIMeta(display_name="Probability threshold", min=0.0, max=1.0),
        ] = None
        nms_threshold: Annotated[
            float | None,
            GUIMeta(display_name="NMS threshold", min=0.0, max=1.0),
        ] = None
        tiles_z: Annotated[int, GUIMeta(display_name="Z tiles", min=1)] = 1
        tiles_y: Annotated[int, GUIMeta(display_name="Y tiles", min=1)] = 1
        tiles_x: Annotated[int, GUIMeta(display_name="X tiles", min=1)] = 1
        anisotropy_z: Annotated[float, GUIMeta(display_name="Z anisotropy", min=0.000001)] = 1.0
        anisotropy_y: Annotated[float, GUIMeta(display_name="Y anisotropy", min=0.000001)] = 1.0
        anisotropy_x: Annotated[float, GUIMeta(display_name="X anisotropy", min=0.000001)] = 1.0
        optimize_snakes: Annotated[bool, GUIMeta(display_name="Optimize snakes")] = True
        otsu_for_snakes: Annotated[bool, GUIMeta(display_name="Otsu snake gradient")] = True
        device: Annotated[
            Literal["auto", "cpu", "cuda"],
            GUIMeta(display_name="Device"),
        ] = "auto"

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.VOLUMETRIC}),
            GUIMeta(display_name="Segmentation mask"),
        ] = Template("{input_volume.stem}_nagini3d_mask.tif")
        probability: Annotated[
            Path,
            ImageSpec(semantics={Semantic.PROBABILITY}, layouts={Layout.VOLUMETRIC}),
            GUIMeta(display_name="Probability map"),
        ] = Template("{input_volume.stem}_nagini3d_probability.tif")
        surfaces: Annotated[
            Path,
            GUIMeta(display_name="Surface archive"),
        ] = Template("{input_volume.stem}_nagini3d_surfaces.npz")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]
        model_provenance: Annotated[
            Path,
            GUIMeta(display_name="Model provenance"),
        ] = Template("{input_volume.stem}_nagini3d_provenance.json")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import json

        import numpy as np
        import tifffile
        import torch  # type: ignore
        import yaml
        from nagini3D.models.model import Nagini3D  # type: ignore
        from nagini3D.models.tools.refinement import (  # type: ignore
            image_to_refinement_grad,
            image_to_refinement_grad_otsu,
        )
        from nagini3D.models.tools.snake_sampler import SnakeSmoothSampler  # type: ignore

        image = validate_image(
            tifffile.imread(arguments.input_volume),
            name="input_volume",
            dimensions=(3,),
        )
        if image.size == 0:
            raise ValueError("input_volume must not be empty.")
        bundle = Path(arguments.model_bundle)
        if not bundle.is_dir():
            raise ValueError(f"model_bundle must be an existing directory: {bundle}")
        config_path = bundle / "config.yaml"
        thresholds_path = bundle / "thresholds.yaml"
        weights_name = str(arguments.weights_filename).strip()
        if not weights_name or Path(weights_name).name != weights_name:
            raise ValueError("weights_filename must be a file name inside model_bundle.")
        weights_path = bundle / weights_name
        for required in (config_path, thresholds_path, weights_path):
            if not required.is_file():
                raise ValueError(f"NAGINI model bundle is missing required file: {required}")

        with config_path.open() as stream:
            config = yaml.safe_load(stream)
        with thresholds_path.open() as stream:
            thresholds = yaml.safe_load(stream)
        try:
            settings = config["settings"]
            model_config = config["model"]
            m1 = int(settings["M1"])
            m2 = int(settings["M2"])
            mean_radius = float(settings["r_mean"])
            probability_threshold = _threshold(
                arguments.probability_threshold,
                thresholds["prob"],
                "probability_threshold",
            )
            nms_threshold = _threshold(
                arguments.nms_threshold,
                thresholds["nms"],
                "nms_threshold",
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid NAGINI config.yaml or thresholds.yaml.") from error
        if m1 < 1 or m2 < 1 or not np.isfinite(mean_radius) or mean_radius <= 0:
            raise ValueError("NAGINI settings M1, M2, and r_mean must be positive.")

        tiles = [
            _positive_int(arguments.tiles_z, "tiles_z"),
            _positive_int(arguments.tiles_y, "tiles_y"),
            _positive_int(arguments.tiles_x, "tiles_x"),
        ]
        anisotropy = [
            _positive_float(arguments.anisotropy_z, "anisotropy_z"),
            _positive_float(arguments.anisotropy_y, "anisotropy_y"),
            _positive_float(arguments.anisotropy_x, "anisotropy_x"),
        ]
        device_name = str(arguments.device)
        if device_name == "auto":
            device_name = "cuda" if torch.cuda.is_available() else "cpu"
        if device_name not in {"cpu", "cuda"}:
            raise ValueError("device must be 'auto', 'cpu', or 'cuda'.")
        if device_name == "cuda" and not torch.cuda.is_available():
            raise ValueError("device='cuda' was requested but CUDA is unavailable.")

        output_parent = Path(arguments.mask).parent
        output_parent.mkdir(parents=True, exist_ok=True)
        model = Nagini3D(
            unet_cfg=model_config,
            P=101,
            M1=m1,
            M2=m2,
            save_path=str(output_parent),
            device=device_name,
            use_scale=bool(settings.get("use_scale", True)),
        )
        model.load_weights(str(weights_path))
        gradient_function = (
            image_to_refinement_grad_otsu
            if bool(arguments.otsu_for_snakes)
            else image_to_refinement_grad
        )
        mask, probability, surface_data = model.inference(
            image,
            proba_th=probability_threshold,
            r_mean=mean_radius,
            nb_tiles=tiles,
            nms_th=nms_threshold,
            optim_snake=bool(arguments.optimize_snakes),
            anisotropy=anisotropy,
            grad_fn=gradient_function,
        )
        mask = validate_labels(
            mask,
            name="NAGINI-3D mask",
            dimensions=(3,),
            expected_shape=image.shape,
        )
        probability = validate_image(
            probability,
            name="NAGINI-3D probability",
            dimensions=(3,),
        )
        if probability.shape != image.shape:
            raise ValueError(
                "NAGINI-3D probability shape must match input_volume; "
                f"got {probability.shape} and {image.shape}."
            )
        count = object_count(mask)
        centers = np.asarray(surface_data["centers"])
        parameters = np.asarray(surface_data["params"])
        if len(centers) != count or len(parameters) != count:
            raise ValueError(
                "NAGINI-3D surface count must match the number of mask objects."
            )
        if count:
            sampler = SnakeSmoothSampler(P=301, M1=m1, M2=m2, device=device_name)
            curvature_positions, curvature_values = sampler.get_curvature_and_position(
                torch.as_tensor(parameters, device=device_name)
            )
            curvature_positions = _as_numpy(curvature_positions)
            curvature_values = _as_numpy(curvature_values)
        else:
            curvature_positions = np.empty((0, 3), dtype=np.float32)
            curvature_values = np.empty((0,), dtype=np.float32)

        mask_path = Path(arguments.mask)
        probability_path = Path(arguments.probability)
        surfaces_path = Path(arguments.surfaces)
        write_labels(mask_path, mask)
        probability_path.parent.mkdir(parents=True, exist_ok=True)
        tifffile.imwrite(
            probability_path,
            np.asarray(probability, dtype=np.float32),
            photometric="minisblack",
        )
        surfaces_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            surfaces_path,
            points=np.asarray(surface_data["points"]),
            facets=np.asarray(surface_data["facets"]),
            values=np.asarray(surface_data["values"]),
            centers=centers,
            params=parameters,
            curvature_positions=curvature_positions,
            curvature_values=curvature_values,
        )

        provenance_path = Path(arguments.model_provenance)
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "adapter": "Nagini3DSegment",
                    "input_sha256": _sha256_file(Path(arguments.input_volume)),
                    "model_bundle": str(bundle.resolve()),
                    "model_bundle_sha256": _digest_directory(bundle),
                    "weights_filename": weights_name,
                    "probability_threshold": probability_threshold,
                    "nms_threshold": nms_threshold,
                    "tiles": tiles,
                    "anisotropy": anisotropy,
                    "optimize_snakes": bool(arguments.optimize_snakes),
                    "otsu_for_snakes": bool(arguments.otsu_for_snakes),
                    "device": device_name,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return self.Outputs(
            mask=mask_path,
            probability=probability_path,
            surfaces=surfaces_path,
            object_count=count,
            model_provenance=provenance_path,
        )


def _threshold(override: Any, default: Any, name: str) -> float:
    import math

    value = float(default if override is None else override)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between zero and one.")
    return value


def _positive_int(value: Any, name: str) -> int:
    import numbers

    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


def _positive_float(value: Any, name: str) -> float:
    import math

    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and greater than zero.")
    return result


def _as_numpy(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    return value


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_directory(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(bytes.fromhex(_sha256_file(child)))
    return digest.hexdigest()
