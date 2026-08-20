from pathlib import Path
import os
import sys
import types

import imageio.v3 as iio
import numpy as np
import pytest
from scipy.ndimage import gaussian_filter, median_filter
from skimage.metrics import mean_squared_error, peak_signal_noise_ratio
from skimage.restoration import denoise_bilateral, richardson_lucy

from bioimageflow import Workflow
from bioimageflow_core import Arguments
import bioimageflow_restoration_tools as restoration_tools
from bioimageflow_restoration_tools import (
    BackgroundSubtract,
    BilateralDenoise,
    CAREamicsPredict,
    GaussianDenoise,
    MedianDenoise,
    RestorationMetrics,
    RichardsonLucyRestoration,
    TotalVariationDenoise,
    UnsharpMask,
)

pytestmark = pytest.mark.package_tools


def _write(path: Path, image: np.ndarray) -> Path:
    iio.imwrite(path, image.astype(np.float32))
    return path


def test_public_exports_are_canonical_tools() -> None:
    assert restoration_tools.__all__ == [
        "BackgroundSubtract",
        "BilateralDenoise",
        "CAREamicsPredict",
        "GaussianDenoise",
        "MedianDenoise",
        "RestorationMetrics",
        "RichardsonLucyRestoration",
        "TotalVariationDenoise",
        "UnsharpMask",
    ]
    assert not hasattr(restoration_tools, "RestoreImage")


def test_total_variation_denoise_improves_noisy_image(tmp_path: Path) -> None:
    clean = np.zeros((48, 48), dtype=np.float32)
    clean[14:34, 14:34] = 1.0
    rng = np.random.default_rng(42)
    noisy = np.clip(clean + rng.normal(0.0, 0.18, clean.shape), 0.0, 1.0)
    noisy_path = _write(tmp_path / "noisy.tif", noisy)
    result = TotalVariationDenoise().process_row(
        Arguments(
            input_image=noisy_path,
            weight=0.12,
            output_image=tmp_path / "restored.tif",
        )
    )
    restored = iio.imread(result.output_image)
    assert restored.shape == noisy.shape
    assert np.mean((restored - clean) ** 2) < np.mean((noisy - clean) ** 2)


def test_bilateral_denoise_matches_scikit_image(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    image = rng.random((16, 17), dtype=np.float32)
    image_path = _write(tmp_path / "input.tif", image)
    result = BilateralDenoise().process_row(
        Arguments(
            input_image=image_path,
            sigma_color=0.2,
            sigma_spatial=2.0,
            output_image=tmp_path / "bilateral.tif",
        )
    )
    expected = denoise_bilateral(
        image, sigma_color=0.2, sigma_spatial=2.0, channel_axis=None
    )
    np.testing.assert_allclose(iio.imread(result.output_image), expected, rtol=1e-6)


def test_restoration_metrics_match_scikit_image(tmp_path: Path) -> None:
    clean = np.linspace(0.0, 1.0, 64, dtype=np.float32).reshape(8, 8)
    degraded = np.clip(clean + 0.1, 0.0, 1.0)
    restored = np.clip(clean + 0.02, 0.0, 1.0)
    clean_path = _write(tmp_path / "clean.tif", clean)
    degraded_path = _write(tmp_path / "degraded.tif", degraded)
    restored_path = _write(tmp_path / "restored.tif", restored)
    metrics = RestorationMetrics().process_row(
        Arguments(
            clean_image=clean_path,
            degraded_image=degraded_path,
            restored_image=restored_path,
            data_range=1.0,
        )
    )
    assert metrics.mse_degraded == pytest.approx(mean_squared_error(clean, degraded))
    assert metrics.mse_restored == pytest.approx(mean_squared_error(clean, restored))
    assert metrics.degraded_psnr == pytest.approx(
        peak_signal_noise_ratio(clean, degraded, data_range=1.0)
    )
    assert metrics.restored_psnr == pytest.approx(
        peak_signal_noise_ratio(clean, restored, data_range=1.0)
    )


def test_metrics_infer_range_and_report_perfect_restoration(tmp_path: Path) -> None:
    clean = np.arange(16, dtype=np.float32).reshape(4, 4)
    degraded = clean + 1.0
    clean_path = _write(tmp_path / "clean.tif", clean)
    degraded_path = _write(tmp_path / "degraded.tif", degraded)
    metrics = RestorationMetrics().process_row(
        Arguments(
            clean_image=clean_path,
            degraded_image=degraded_path,
            restored_image=clean_path,
            data_range=None,
        )
    )
    assert metrics.mse_restored == 0.0
    assert metrics.restored_psnr == float("inf")
    assert metrics.degraded_psnr == pytest.approx(
        peak_signal_noise_ratio(clean, degraded, data_range=15.0)
    )


def test_metrics_require_range_for_constant_reference(tmp_path: Path) -> None:
    constant = _write(tmp_path / "constant.tif", np.ones((5, 5), dtype=np.float32))
    with pytest.raises(ValueError, match="constant"):
        RestorationMetrics().process_row(
            Arguments(
                clean_image=constant,
                degraded_image=constant,
                restored_image=constant,
                data_range=None,
            )
        )
    metrics = RestorationMetrics().process_row(
        Arguments(
            clean_image=constant,
            degraded_image=constant,
            restored_image=constant,
            data_range=1.0,
        )
    )
    assert metrics.mse_degraded == 0.0
    assert metrics.degraded_psnr == float("inf")


def test_metrics_reject_non_planar_images(tmp_path: Path) -> None:
    volume = _write(tmp_path / "volume.tif", np.zeros((2, 5, 5), dtype=np.float32))
    with pytest.raises(ValueError, match="2D"):
        RestorationMetrics().process_row(
            Arguments(
                clean_image=volume,
                degraded_image=volume,
                restored_image=volume,
                data_range=1.0,
            )
        )


@pytest.mark.parametrize("failure", ["shape", "nan"])
def test_metrics_reject_invalid_images(tmp_path: Path, failure: str) -> None:
    clean = np.zeros((5, 5), dtype=np.float32)
    degraded = np.zeros((5, 5), dtype=np.float32)
    restored = np.zeros((5, 5), dtype=np.float32)
    if failure == "shape":
        restored = np.zeros((4, 5), dtype=np.float32)
        match = "must match"
    else:
        degraded[0, 0] = np.nan
        match = "finite"
    with pytest.raises(ValueError, match=match):
        RestorationMetrics().process_row(
            Arguments(
                clean_image=_write(tmp_path / "clean.tif", clean),
                degraded_image=_write(tmp_path / "degraded.tif", degraded),
                restored_image=_write(tmp_path / "restored.tif", restored),
                data_range=1.0,
            )
        )


def test_careamics_uses_pinned_careamist_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    module = types.ModuleType("careamics")

    class FakeCAREamist:
        def __init__(
            self, *, checkpoint_path: Path, enable_progress_bar: bool
        ) -> None:
            calls["checkpoint_path"] = checkpoint_path
            calls["enable_progress_bar"] = enable_progress_bar

        def predict(
            self, *, pred_data: np.ndarray, axes: str, data_type: str
        ) -> tuple[list[np.ndarray], list[str]]:
            calls["pred_data"] = pred_data
            calls["axes"] = axes
            calls["data_type"] = data_type
            return [pred_data + 1.0], []

    module.CAREamist = FakeCAREamist
    monkeypatch.setitem(sys.modules, "careamics", module)
    image_path = _write(tmp_path / "input.tif", np.zeros((8, 8), dtype=np.float32))
    checkpoint = tmp_path / "model.ckpt"
    checkpoint.touch()
    result = CAREamicsPredict().process_row(
        Arguments(
            input_image=image_path,
            output_image=tmp_path / "restored.tif",
            checkpoint=checkpoint,
        )
    )
    assert calls["checkpoint_path"] == checkpoint
    assert calls["enable_progress_bar"] is False
    assert calls["axes"] == "YX"
    assert calls["data_type"] == "array"
    assert result.model_source == str(checkpoint)
    np.testing.assert_array_equal(iio.imread(result.output_image), np.ones((8, 8)))


@pytest.mark.parametrize(
    ("predictions", "match"),
    [
        ([], "exactly one"),
        ([np.zeros((4, 4)), np.zeros((4, 4))], "exactly one"),
        ([np.zeros((3, 4))], "shape"),
    ],
)
def test_careamics_validates_prediction_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    predictions: list[np.ndarray],
    match: str,
) -> None:
    module = types.ModuleType("careamics")

    class FakeCAREamist:
        def __init__(
            self, *, checkpoint_path: Path, enable_progress_bar: bool
        ) -> None:
            pass

        def predict(
            self, *, pred_data: np.ndarray, axes: str, data_type: str
        ) -> tuple[list[np.ndarray], list[str]]:
            return predictions, []

    module.CAREamist = FakeCAREamist
    monkeypatch.setitem(sys.modules, "careamics", module)
    image_path = _write(tmp_path / "input.tif", np.zeros((4, 4), dtype=np.float32))
    checkpoint = tmp_path / "model.ckpt"
    checkpoint.touch()
    with pytest.raises(ValueError, match=match):
        CAREamicsPredict().process_row(
            Arguments(
                input_image=image_path,
                output_image=tmp_path / "restored.tif",
                checkpoint=checkpoint,
            )
        )


@pytest.mark.model_runtime
def test_careamics_with_configured_checkpoint(tmp_path: Path) -> None:
    pytest.importorskip("careamics")
    checkpoint_value = os.environ.get("BIOIMAGEFLOW_CAREAMICS_CHECKPOINT")
    if checkpoint_value is None:
        pytest.skip("Set BIOIMAGEFLOW_CAREAMICS_CHECKPOINT to run this smoke test.")
    image_path = _write(tmp_path / "input.tif", np.zeros((64, 64), dtype=np.float32))
    result = CAREamicsPredict().process_row(
        Arguments(
            input_image=image_path,
            checkpoint=Path(checkpoint_value),
            output_image=tmp_path / "prediction.tif",
        )
    )
    prediction = iio.imread(result.output_image)
    assert prediction.shape == (64, 64)
    assert np.isfinite(prediction).all()


def test_restoration_workflow_graph_runs(tmp_path: Path) -> None:
    image = np.zeros((32, 32), dtype=np.float32)
    image[8:24, 8:24] = 1.0
    image_path = _write(tmp_path / "image.tif", image)
    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as workflow:
        restored = TotalVariationDenoise()(input_image=image_path, name="restore")
        result = workflow.compute(restored)
    assert Path(result.iloc[0]["output_image"]).exists()


def test_gaussian_and_median_match_scipy(tmp_path: Path) -> None:
    image = np.zeros((21, 21), dtype=np.float32)
    image[10, 10] = 10.0
    image[2, 2] = 8.0
    image_path = _write(tmp_path / "impulse.tif", image)
    gaussian = GaussianDenoise().process_row(
        Arguments(input_image=image_path, sigma=1.0, output_image=tmp_path / "gaussian.tif")
    )
    median = MedianDenoise().process_row(
        Arguments(input_image=image_path, radius=1, output_image=tmp_path / "median.tif")
    )
    np.testing.assert_allclose(
        iio.imread(gaussian.output_image),
        gaussian_filter(image, sigma=1.0, mode="nearest"),
    )
    np.testing.assert_array_equal(
        iio.imread(median.output_image),
        median_filter(image, size=3, mode="nearest"),
    )


def test_background_subtract_shift_to_zero_is_explicit(tmp_path: Path) -> None:
    image = np.ones((24, 24), dtype=np.float32) * 5.0
    image[8:16, 8:16] = 8.0
    image_path = _write(tmp_path / "contrast.tif", image)
    shifted = BackgroundSubtract().process_row(
        Arguments(
            input_image=image_path,
            sigma=3.0,
            shift_to_zero=True,
            output_image=tmp_path / "shifted.tif",
        )
    )
    signed = BackgroundSubtract().process_row(
        Arguments(
            input_image=image_path,
            sigma=3.0,
            shift_to_zero=False,
            output_image=tmp_path / "signed.tif",
        )
    )
    shifted_image = iio.imread(shifted.output_image)
    signed_image = iio.imread(signed.output_image)
    assert shifted_image.min() == pytest.approx(0.0)
    assert signed_image.min() < 0.0
    np.testing.assert_allclose(shifted_image, signed_image - signed_image.min())


def test_unsharp_mask_increases_edge_contrast(tmp_path: Path) -> None:
    image = np.ones((24, 24), dtype=np.float32) * 5.0
    image[8:16, 8:16] = 8.0
    image_path = _write(tmp_path / "contrast.tif", image)
    result = UnsharpMask().process_row(
        Arguments(
            input_image=image_path,
            sigma=1.0,
            amount=1.0,
            output_image=tmp_path / "unsharp.tif",
        )
    )
    assert iio.imread(result.output_image)[8, 8] > image[8, 8]


def test_richardson_lucy_matches_scikit_image(tmp_path: Path) -> None:
    image = np.zeros((15, 15), dtype=np.float32)
    image[7, 7] = 1.0
    psf = np.ones((3, 3), dtype=np.float32) / 9.0
    image_path = _write(tmp_path / "blurred.tif", image)
    psf_path = _write(tmp_path / "psf.tif", psf)
    result = RichardsonLucyRestoration().process_row(
        Arguments(
            input_image=image_path,
            psf_image=psf_path,
            iterations=3,
            clip=False,
            output_image=tmp_path / "rl.tif",
        )
    )
    expected = richardson_lucy(image, psf / psf.sum(), num_iter=3, clip=False)
    np.testing.assert_allclose(iio.imread(result.output_image), expected, rtol=1e-6)


@pytest.mark.parametrize(
    ("tool", "arguments", "match"),
    [
        (GaussianDenoise(), {"sigma": 0.0}, "sigma"),
        (MedianDenoise(), {"radius": 1.5}, "radius"),
        (BackgroundSubtract(), {"sigma": -1.0, "shift_to_zero": True}, "sigma"),
        (UnsharpMask(), {"sigma": 1.0, "amount": -0.1}, "amount"),
        (TotalVariationDenoise(), {"weight": 0.0}, "weight"),
        (
            BilateralDenoise(),
            {"sigma_color": 0.1, "sigma_spatial": float("inf")},
            "sigma_spatial",
        ),
    ],
)
def test_classical_tools_reject_invalid_parameters(
    tmp_path: Path,
    tool: object,
    arguments: dict[str, object],
    match: str,
) -> None:
    image_path = _write(tmp_path / "image.tif", np.ones((8, 8), dtype=np.float32))
    with pytest.raises(ValueError, match=match):
        tool.process_row(  # type: ignore[attr-defined]
            Arguments(
                input_image=image_path,
                output_image=tmp_path / "output.tif",
                **arguments,
            )
        )


@pytest.mark.parametrize("invalid_psf", [np.zeros((3, 3)), -np.ones((3, 3))])
def test_richardson_lucy_rejects_invalid_psf(
    tmp_path: Path, invalid_psf: np.ndarray
) -> None:
    image_path = _write(tmp_path / "image.tif", np.ones((8, 8), dtype=np.float32))
    psf_path = _write(tmp_path / "psf.tif", invalid_psf)
    with pytest.raises(ValueError, match="psf_image"):
        RichardsonLucyRestoration().process_row(
            Arguments(
                input_image=image_path,
                psf_image=psf_path,
                iterations=3,
                clip=False,
                output_image=tmp_path / "output.tif",
            )
        )


def test_richardson_lucy_rejects_invalid_iterations(tmp_path: Path) -> None:
    image_path = _write(tmp_path / "image.tif", np.ones((8, 8), dtype=np.float32))
    with pytest.raises(ValueError, match="iterations"):
        RichardsonLucyRestoration().process_row(
            Arguments(
                input_image=image_path,
                psf_image=None,
                iterations=0,
                clip=False,
                output_image=tmp_path / "output.tif",
            )
        )
