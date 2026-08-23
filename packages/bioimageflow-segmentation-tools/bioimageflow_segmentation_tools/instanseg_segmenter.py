"""Production InstanSeg inference adapter."""

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


instanseg_env = EnvironmentSpec(
    name="segmentation-instanseg",
    dependencies={
        "python": "3.11",
        "pip": [
            "instanseg-torch==0.1.1",
            "imageio==2.37.3",
            "numpy==2.2.6",
            "scikit-image==0.25.2",
            "tifffile==2025.5.10",
            "torch==2.7.1",
        ],
    },
)


class InstanSegSegment(ProcessingTool):
    """Segment nuclei or cells with an InstanSeg model."""

    row_consumption = RowConsumption.MAPPED
    display_name = "InstanSeg"
    documentation = (
        "Segment nuclei or cells in a 2D microscopy image using a named "
        "InstanSeg model or a local model bundle."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "instanseg", "nuclei", "cells", "deep learning"]
    environment = instanseg_env

    def __init__(self) -> None:
        super().__init__()
        self._model_cache_key: tuple[str, str | None] | None = None
        self._cached_model: Any | None = None

    def clear_model_cache(self) -> None:
        """Release the cached model held by this tool instance."""
        self._model_cache_key = None
        self._cached_model = None

    def _get_model(self, model_source: str, device: str | None) -> Any:
        key = (model_source, device)
        if self._cached_model is None or self._model_cache_key != key:
            from instanseg import InstanSeg  # type: ignore

            self.clear_model_cache()
            self._cached_model = InstanSeg(
                model_type=model_source,
                device=device,
                image_reader="skimage.io",
                verbosity=0,
            )
            self._model_cache_key = key
        return self._cached_model

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR, Layout.PLANAR_CHANNEL},
            ),
            GUIMeta(
                display_name="Input image",
                description="2D intensity image, optionally with a channel axis.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        model_name: Annotated[
            str,
            GUIMeta(
                display_name="Model name",
                description="Official InstanSeg model name used when model_path is empty.",
            ),
        ] = "fluorescence_nuclei_and_cells"
        model_path: Annotated[
            Path | None,
            GUIMeta(
                display_name="Local model",
                description="Optional local model bundle. Takes precedence over model_name.",
                connectable=Connectable.NEVER,
            ),
        ] = None
        target: Annotated[
            Literal["nuclei", "cells"],
            GUIMeta(
                display_name="Target",
                description="Instance type returned in the single output mask.",
            ),
        ] = "nuclei"
        pixel_size_um: Annotated[
            float | None,
            GUIMeta(
                display_name="Pixel size",
                description="Optional physical pixel size in micrometres.",
                min=0.000001,
            ),
        ] = None
        channel_axis: Annotated[
            Literal["first", "last"],
            GUIMeta(
                display_name="Channel axis",
                description="Location of the channel axis for a three-dimensional array.",
            ),
        ] = "first"
        channel_ids: Annotated[
            list[int] | None,
            GUIMeta(
                display_name="Channels",
                description="Optional zero-based channels passed to InstanSeg in this order.",
            ),
        ] = None
        processing_method: Annotated[
            Literal["auto", "small", "medium"],
            GUIMeta(
                display_name="Processing method",
                description="Whole-image, tiled, or automatically selected inference.",
            ),
        ] = "auto"
        device: Annotated[
            Literal["auto", "cpu", "cuda", "mps"],
            GUIMeta(display_name="Device"),
        ] = "auto"

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Segmentation mask"),
        ] = Template("{input_image.stem}_instanseg_{target}_mask.tif")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]
        model_source: Annotated[str, GUIMeta(display_name="Model source")]
        model_provenance: Annotated[
            Path,
            GUIMeta(display_name="Model provenance"),
        ] = Template("{input_image.stem}_instanseg_{target}_provenance.json")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import json

        import imageio.v3 as iio
        import numpy as np

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        if image.ndim == 2:
            prepared = image
            channel_count = 1
        else:
            axis = 0 if str(arguments.channel_axis) == "first" else image.ndim - 1
            channel_count = int(image.shape[axis])
            if channel_count < 1:
                raise ValueError("input_image channel axis must not be empty.")
            prepared = np.moveaxis(image, axis, 0)

        channel_ids = arguments.channel_ids
        if channel_ids is not None:
            if not channel_ids:
                raise ValueError("channel_ids must not be empty when provided.")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, np.integer))
                or int(value) < 0
                or int(value) >= channel_count
                for value in channel_ids
            ):
                raise ValueError(
                    f"channel_ids must be unique zero-based indices below {channel_count}."
                )
            if len(set(int(value) for value in channel_ids)) != len(channel_ids):
                raise ValueError("channel_ids must not contain duplicates.")
            normalized_channel_ids = [int(value) for value in channel_ids]
        else:
            normalized_channel_ids = None

        pixel_size = arguments.pixel_size_um
        if pixel_size is not None:
            pixel_size = float(pixel_size)
            if not np.isfinite(pixel_size) or pixel_size <= 0.0:
                raise ValueError("pixel_size_um must be finite and greater than zero.")

        target = str(arguments.target)
        if target not in {"nuclei", "cells"}:
            raise ValueError("target must be 'nuclei' or 'cells'.")
        processing_method = str(arguments.processing_method)
        if processing_method not in {"auto", "small", "medium"}:
            raise ValueError("processing_method must be 'auto', 'small', or 'medium'.")
        device_name = str(arguments.device)
        if device_name not in {"auto", "cpu", "cuda", "mps"}:
            raise ValueError("device must be 'auto', 'cpu', 'cuda', or 'mps'.")
        device = None if device_name == "auto" else device_name

        model_path = arguments.model_path
        if model_path is not None:
            local_model = Path(model_path)
            if not local_model.is_dir() or not (local_model / "instanseg.pt").is_file():
                raise ValueError(
                    "model_path must be an InstanSeg model directory containing "
                    f"instanseg.pt: {local_model}"
                )
            model_source = str(local_model.resolve())
            model_kind = "local"
            model_digest = _digest_path(local_model)
            model_version = None
            model_url = None
        else:
            model_source = str(arguments.model_name).strip()
            if not model_source:
                raise ValueError("model_name must not be empty when model_path is not set.")
            model_kind = "named"
            model_version = None
            model_url = None
            model_digest = None

        model = self._get_model(model_source, device)
        if model_kind == "named":
            model_version, model_url, model_digest = _named_model_metadata(model_source)
        cells_and_nuclei = bool(getattr(model.instanseg, "cells_and_nuclei", False))
        if target == "cells" and not cells_and_nuclei:
            raise ValueError(
                "The selected InstanSeg model does not provide cell segmentation."
            )

        kwargs: dict[str, Any] = {
            "pixel_size": pixel_size,
            "return_image_tensor": False,
            "target": target,
        }
        if normalized_channel_ids is not None:
            kwargs["channel_ids"] = normalized_channel_ids
        method = processing_method
        if method == "auto":
            method = "small" if int(np.prod(prepared.shape)) <= 3 * 1500 * 1500 else "medium"
        if method == "small":
            prediction = model.eval_small_image(prepared, **kwargs)
        else:
            prediction = model.eval_medium_image(prepared, **kwargs)

        labels = _instanseg_labels(prediction)
        labels = validate_labels(
            labels,
            name="InstanSeg mask",
            dimensions=(2,),
            expected_shape=tuple(int(size) for size in prepared.shape[-2:]),
        )
        mask_path = Path(arguments.mask)
        write_labels(mask_path, labels)

        provenance = {
            "adapter": "InstanSegSegment",
            "input_sha256": _sha256_file(Path(arguments.input_image)),
            "model_kind": model_kind,
            "model_source": model_source,
            "model_version": model_version,
            "model_url": model_url,
            "model_sha256": model_digest,
            "target": target,
            "pixel_size_um": pixel_size,
            "channel_ids": normalized_channel_ids,
            "processing_method": method,
            "device": device_name,
        }
        provenance_path = Path(arguments.model_provenance)
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
        return self.Outputs(
            mask=mask_path,
            object_count=object_count(labels),
            model_source=model_source,
            model_provenance=provenance_path,
        )


def _instanseg_labels(prediction: Any) -> Any:
    import numpy as np

    if hasattr(prediction, "detach"):
        prediction = prediction.detach().cpu().numpy()
    labels = np.asarray(prediction)
    labels = np.squeeze(labels)
    if labels.ndim != 2:
        raise ValueError(
            "InstanSeg selected-target prediction must contain one 2D label image; "
            f"got shape {labels.shape}."
        )
    return labels


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_path(path: Path) -> str:
    import hashlib

    if path.is_file():
        return _sha256_file(path)
    digest = hashlib.sha256()
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(bytes.fromhex(_sha256_file(child)))
    return digest.hexdigest()


def _named_model_metadata(model_name: str) -> tuple[str | None, str | None, str | None]:
    import importlib.resources
    import json
    import os

    try:
        index_path = importlib.resources.files("instanseg").joinpath(
            "bioimageio_models/model-index.json"
        )
        models = json.loads(index_path.read_text())
    except (AttributeError, FileNotFoundError, ModuleNotFoundError, TypeError):
        return None, None, None
    matches = [model for model in models if model.get("name") == model_name]
    if not matches:
        return None, None, None
    selected = matches[0]
    version = str(selected["version"])
    url = str(selected["url"])
    base = os.environ.get("INSTANSEG_BIOIMAGEIO_PATH")
    if base is None:
        return version, url, None
    weights = Path(base) / model_name / version / "instanseg.pt"
    digest = _sha256_file(weights) if weights.is_file() else None
    return version, url, digest
