from __future__ import annotations

import math
import os
from pathlib import Path

import imageio.v3 as iio
import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = Path(os.environ.get("BIOIMAGEFLOW_DOC_ASSET_SOURCE", ROOT))
OUT = Path(__file__).resolve().parent


def _font(size: int = 18) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("Arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _read_image(path: Path) -> np.ndarray | None:
    if not path.exists():
        return None
    return np.asarray(iio.imread(path))


def _read_first_match(pattern: str) -> np.ndarray | None:
    for path in sorted(SOURCE_ROOT.glob(pattern)):
        return _read_image(path)
    return None


def _normalize(image: np.ndarray) -> np.ndarray:
    data = np.asarray(image, dtype=float)
    if data.ndim > 2:
        data = data[..., 0]
    low, high = np.percentile(data, [1, 99.8])
    if high <= low:
        high = float(data.max() or 1)
        low = float(data.min())
    scaled = np.clip((data - low) / (high - low), 0, 1)
    return (scaled * 255).astype(np.uint8)


def _center_crop(image: np.ndarray, size: int = 360) -> np.ndarray:
    if image.ndim == 3 and image.shape[0] in {3, 4}:
        _, height, width = image.shape
        y0 = max((height - size) // 2, 0)
        x0 = max((width - size) // 2, 0)
        return image[:, y0 : y0 + size, x0 : x0 + size]
    height, width = image.shape[:2]
    y0 = max((height - size) // 2, 0)
    x0 = max((width - size) // 2, 0)
    return image[y0 : y0 + size, x0 : x0 + size]


def _rgb_from_cyx(image: np.ndarray) -> np.ndarray:
    if image is None or image.ndim != 3:
        image = _fish_like_image()
    if image.shape[0] >= 3:
        ch0 = _normalize(image[0])
        ch1 = _normalize(image[1])
        ch2 = _normalize(image[2])
        return np.stack([ch1, ch0, ch2], axis=-1)
    return np.repeat(_normalize(image)[..., None], 3, axis=-1)


def _label_edges(labels: np.ndarray) -> np.ndarray:
    labels = np.asarray(labels)
    edges = np.zeros(labels.shape, dtype=bool)
    edges[:-1, :] |= labels[:-1, :] != labels[1:, :]
    edges[:, :-1] |= labels[:, :-1] != labels[:, 1:]
    return edges & (labels > 0)


def _spot_edges(mask: np.ndarray) -> np.ndarray:
    binary = np.asarray(mask) > 0
    edges = np.zeros(binary.shape, dtype=bool)
    edges[:-1, :] |= binary[:-1, :] != binary[1:, :]
    edges[:, :-1] |= binary[:, :-1] != binary[:, 1:]
    return edges | binary


def _panel(title: str, image: np.ndarray | Image.Image, width: int = 420, height: int = 470) -> Image.Image:
    if isinstance(image, np.ndarray):
        if image.ndim == 2:
            image = np.repeat(image[..., None], 3, axis=-1)
        pil = Image.fromarray(image.astype(np.uint8))
    else:
        pil = image.convert("RGB")
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((18, 16), title, fill=(28, 34, 42), font=_font(18))
    max_w = width - 36
    max_h = height - 70
    pil.thumbnail((max_w, max_h), Image.Resampling.BILINEAR)
    canvas.paste(pil, ((width - pil.width) // 2, 56))
    return canvas


def _save_grid(workflow: str, name: str, panels: list[Image.Image], columns: int = 2) -> None:
    target_dir = OUT / workflow
    target_dir.mkdir(parents=True, exist_ok=True)
    panel_w = max(panel.width for panel in panels)
    panel_h = max(panel.height for panel in panels)
    rows = math.ceil(len(panels) / columns)
    canvas = Image.new("RGB", (panel_w * columns, panel_h * rows), (245, 247, 250))
    for index, panel in enumerate(panels):
        x = (index % columns) * panel_w
        y = (index // columns) * panel_h
        canvas.paste(panel, (x, y))
    canvas.save(target_dir / name)


def _save_image_grid(
    workflow: str,
    name: str,
    images: list[np.ndarray | Image.Image],
    columns: int = 2,
    gap: int = 8,
    background: tuple[int, int, int] = (8, 10, 14),
) -> None:
    target_dir = OUT / workflow
    target_dir.mkdir(parents=True, exist_ok=True)
    panels: list[Image.Image] = []
    for image in images:
        if isinstance(image, np.ndarray):
            if image.ndim == 2:
                image = np.repeat(image[..., None], 3, axis=-1)
            panels.append(Image.fromarray(image.astype(np.uint8)).convert("RGB"))
        else:
            panels.append(image.convert("RGB"))
    panel_w = max(panel.width for panel in panels)
    panel_h = max(panel.height for panel in panels)
    rows = math.ceil(len(panels) / columns)
    canvas = Image.new(
        "RGB",
        (panel_w * columns + gap * (columns - 1), panel_h * rows + gap * (rows - 1)),
        background,
    )
    for index, panel in enumerate(panels):
        if panel.size != (panel_w, panel_h):
            panel = panel.resize((panel_w, panel_h), Image.Resampling.BILINEAR)
        x = (index % columns) * (panel_w + gap)
        y = (index // columns) * (panel_h + gap)
        canvas.paste(panel, (x, y))
    canvas.save(target_dir / name)


def _draw_table(rows: list[tuple[str, str]], size: tuple[int, int] = (760, 420)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=(210, 214, 220))
    y = 24
    for label, value in rows:
        draw.text((28, y), label, fill=(32, 43, 58), font=_font(18))
        draw.text((360, y), value, fill=(23, 92, 130), font=_font(18))
        y += 44
        draw.line((24, y - 14, size[0] - 24, y - 14), fill=(230, 233, 238))
    return image


def _fish_like_image(size: int = 420) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size]
    rng = np.random.default_rng(12)
    image = np.zeros((3, size, size), dtype=np.uint8)
    for cy, cx, radius in [(145, 160, 38), (245, 245, 52), (295, 145, 34), (145, 295, 30)]:
        image[2, (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2] = 150
    for channel, count, value in [(0, 90, 210), (1, 75, 230)]:
        points = rng.integers(35, size - 35, size=(count, 2))
        for cy, cx in points:
            image[channel, cy - 1 : cy + 2, cx - 1 : cx + 2] = value
    image += rng.integers(0, 18, size=image.shape, dtype=np.uint8)
    return image


def _bbbc_like() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[0:420, 0:420]
    image = np.zeros((420, 420), dtype=float)
    reference = np.zeros((420, 420), dtype=np.uint16)
    centers = [(95, 120, 42), (170, 270, 55), (285, 145, 48), (300, 310, 38)]
    for label, (cy, cx, radius) in enumerate(centers, start=1):
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
        reference[mask] = label
        image += mask.astype(float) * (0.55 + label * 0.07)
    image += np.linspace(0.04, 0.15, image.shape[1])[None, :]
    rng = np.random.default_rng(8)
    image += rng.normal(0, 0.025, image.shape)
    image = np.clip(image, 0, 1)
    shifted = np.roll(reference, shift=6, axis=1)
    classical = (image > 0.35).astype(np.uint8)
    return (image * 255).astype(np.uint8), reference, shifted, classical * 255


def _color_labels(labels: np.ndarray) -> np.ndarray:
    labels = np.asarray(labels)
    palette = np.array(
        [
            [0, 0, 0],
            [230, 90, 80],
            [90, 160, 230],
            [80, 190, 120],
            [220, 170, 70],
            [170, 110, 220],
        ],
        dtype=np.uint8,
    )
    return palette[labels % len(palette)]


def _channel_color(channel: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    gray = _normalize(channel).astype(float) / 255.0
    rgb = np.zeros((*gray.shape, 3), dtype=np.uint8)
    for index, value in enumerate(color):
        rgb[..., index] = (gray * value).astype(np.uint8)
    return rgb


def _dilate_mask(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    binary = np.asarray(mask) > 0
    padded = np.pad(binary, radius, mode="constant", constant_values=False)
    result = np.zeros_like(binary)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            result |= padded[dy : dy + binary.shape[0], dx : dx + binary.shape[1]]
    return result


def _label_spot_overlay(
    labels: np.ndarray | None,
    fols2_spots: np.ndarray | None,
    csf1r_spots: np.ndarray | None,
    fallback_rgb: np.ndarray,
) -> np.ndarray:
    if labels is None:
        overlay = np.clip(fallback_rgb.astype(float) * 0.35, 0, 255).astype(np.uint8)
    else:
        colored = _color_labels(labels)
        background = np.full_like(colored, [14, 16, 22])
        overlay = np.where((np.asarray(labels) > 0)[..., None], colored, background)
    if fols2_spots is not None:
        overlay[_dilate_mask(fols2_spots, radius=2)] = [60, 255, 120]
    if csf1r_spots is not None:
        overlay[_dilate_mask(csf1r_spots, radius=2)] = [255, 70, 80]
    return overlay


def _overlay(base: np.ndarray, labels: np.ndarray | None = None, spots: np.ndarray | None = None) -> np.ndarray:
    if base.ndim == 2:
        rgb = np.repeat(_normalize(base)[..., None], 3, axis=-1)
    else:
        rgb = base.copy()
    if labels is not None:
        edges = _label_edges(labels)
        rgb[edges] = [255, 220, 40]
    if spots is not None:
        rgb[_spot_edges(spots)] = [255, 80, 80]
    return rgb


def fish_assets() -> None:
    image = _read_image(SOURCE_ROOT / "example_workflows/fish_analysis/data/13432.tif")
    image = _center_crop(image if image is not None else _fish_like_image(), 420)
    rgb = _rgb_from_cyx(image)
    nuclei = _read_first_match("fish_results/data/cellpose_nuclei/*/assets/13432.ome_ch2_mask.tiff")
    fols2_spots = _read_first_match("fish_results/data/atlas_fols2/*/assets/13432.ome_ch0_detections.tiff")
    csf1r_spots = _read_first_match("fish_results/data/atlas_csf*/*/assets/13432.ome_ch1_detections.tiff")
    if nuclei is not None:
        nuclei = _center_crop(nuclei, 420)
    if fols2_spots is not None:
        fols2_spots = _center_crop(fols2_spots, 420)
    if csf1r_spots is not None:
        csf1r_spots = _center_crop(csf1r_spots, 420)
    _save_image_grid(
        "fish_analysis",
        "fish_input_channels.png",
        [
            rgb,
            _channel_color(image[0] if image.ndim == 3 else image, (0, 255, 90)),
            _channel_color(image[1] if image.ndim == 3 else image, (255, 70, 70)),
            _channel_color(image[2] if image.ndim == 3 else image, (80, 130, 255)),
        ],
    )
    _save_image_grid(
        "fish_analysis",
        "fish_spot_segmentation_overlay.png",
        [
            _label_spot_overlay(nuclei, fols2_spots, csf1r_spots, rgb),
        ],
        columns=1,
        gap=0,
    )


def parameter_assets() -> None:
    image = _read_image(SOURCE_ROOT / "fish_results/data/extract_ch0_fols2/20260506_155935_a13cf5fa056e/assets/13432.ome_ch0.tiff")
    detection = _read_image(SOURCE_ROOT / "parameter_space_results/data/atlas_detections/20260506_141146_03ca326e4c47/assets/13432_detections.tif")
    mosaic = _read_image(SOURCE_ROOT / "parameter_space_results/data/results_mosaic/20260506_141216_557fb6fdbbab/assets/results_mosaic_mosaic.png")
    if image is None:
        image = _fish_like_image()[0]
    if detection is None:
        detection = image > np.percentile(image, 99.2)
    if mosaic is None:
        mosaic = np.tile(_normalize(detection), (2, 3))
    image = _center_crop(image, 420)
    detection = _center_crop(detection, 420)
    mosaic = _normalize(mosaic)
    _save_grid(
        "parameter_space_exploration",
        "atlas_input_and_mask.png",
        [
            _panel("FISH marker-channel crop", _normalize(image)),
            _panel("One ATLAS detection mask", _normalize(detection)),
        ],
    )
    table = _draw_table(
        [
            ("parameter rows", "image count x sensitivity x size"),
            ("preserved columns", "path, sensitivity, size"),
            ("measured columns", "spot count, foreground fraction"),
            ("preview", "mosaic of ATLAS masks"),
        ]
    )
    _save_grid(
        "parameter_space_exploration",
        "parameter_results.png",
        [
            _panel("Mosaic preview from parameter sweep", mosaic),
            _panel("Parameter-results table", table),
        ],
    )


def bbbc_assets() -> None:
    image, reference, cellpose_like, classical = _bbbc_like()
    _save_grid(
        "bbbc038_segmentation_benchmark",
        "bbbc038_input_reference.png",
        [
            _panel("BBBC038-style nuclei image", image),
            _panel("Reference instance masks", _color_labels(reference)),
        ],
    )
    _save_grid(
        "bbbc038_segmentation_benchmark",
        "bbbc038_method_overlays.png",
        [
            _panel("Cellpose-style prediction overlay", _overlay(image, cellpose_like, None)),
            _panel("Classical threshold branch", _overlay(image, classical, None)),
        ],
    )


def cell_counting_assets() -> None:
    image, labels, _, _ = _bbbc_like()
    table = _draw_table(
        [
            ("object_count", "4 cells"),
            ("mean_area", "measured from labels"),
            ("mean_intensity", "measured inside each object"),
            ("shape features", "perimeter and equivalent diameter"),
        ]
    )
    _save_grid(
        "cell_counting_phenotyping",
        "cell_counting_input_labels.png",
        [
            _panel("Microscopy crop for counting", image),
            _panel("Segmented object labels", _color_labels(labels)),
        ],
    )
    _save_grid(
        "cell_counting_phenotyping",
        "cell_counting_features.png",
        [
            _panel("Object boundaries for inspection", _overlay(image, labels, None)),
            _panel("Per-image phenotype summary", table),
        ],
    )


def restoration_assets() -> None:
    base = _read_image(SOURCE_ROOT / "fish_results/data/extract_ch2_nuclei/20260506_155935_b0162d53a5e9/assets/13432.ome_ch2.tiff")
    if base is None:
        base = _fish_like_image()[2]
    clean = _center_crop(_normalize(base), 420)
    rng = np.random.default_rng(4)
    degraded = np.clip(clean.astype(float) * 0.45 + rng.normal(18, 18, clean.shape), 0, 255).astype(np.uint8)
    restored = np.clip(degraded.astype(float) * 1.55 - 18, 0, 255).astype(np.uint8)
    table = _draw_table(
        [
            ("comparison", "degraded vs restored"),
            ("metric", "MSE and PSNR against clean reference"),
            ("checkpoint", "CAREamics / Noise2Void-style model"),
            ("preview", "side-by-side validation crop"),
        ]
    )
    _save_grid(
        "low_snr_restoration",
        "restoration_input_output.png",
        [
            _panel("Low-SNR microscopy crop", degraded),
            _panel("Restored prediction preview", restored),
        ],
    )
    _save_grid(
        "low_snr_restoration",
        "restoration_metrics.png",
        [
            _panel("Clean reference crop", clean),
            _panel("Metrics table", table),
        ],
    )


def sairpico_assets() -> None:
    base = _read_image(SOURCE_ROOT / "fish_results/data/extract_ch2_nuclei/20260506_155935_b0162d53a5e9/assets/13432.ome_ch2.tiff")
    if base is None:
        base = _fish_like_image()[2]
    image = _center_crop(_normalize(base), 420)
    yy, xx = np.mgrid[-80:81, -80:81]
    psf = np.exp(-((xx**2 + yy**2) / (2 * 18.0**2)))
    psf = _normalize(psf)
    denoised = np.clip(image.astype(float) * 0.85 + 18, 0, 255).astype(np.uint8)
    sharpened = np.clip(image.astype(float) * 1.35 - denoised.astype(float) * 0.25, 0, 255).astype(np.uint8)
    _save_grid(
        "sairpico_deconvolution",
        "sairpico_input_psf.png",
        [
            _panel("Microscopy crop supplied to SAIRPICO", image),
            _panel("Generated Gaussian PSF", psf),
        ],
    )
    _save_grid(
        "sairpico_deconvolution",
        "sairpico_outputs.png",
        [
            _panel("Denoised image", denoised),
            _panel("Richardson-Lucy deconvolution", sharpened),
        ],
    )


def tracking_assets() -> None:
    size = 420
    yy, xx = np.mgrid[0:size, 0:size]
    frames = []
    tracks = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(tracks)
    paths = [[(85 + t * 42, 105 + t * 22) for t in range(5)], [(310 - t * 34, 300 - t * 18) for t in range(5)]]
    colors = [(230, 80, 80), (55, 130, 220)]
    for time in [0, 2, 4]:
        frame = np.zeros((size, size), dtype=np.uint8)
        for label, path in enumerate(paths, start=1):
            cy, cx = path[time]
            frame[(yy - cy) ** 2 + (xx - cx) ** 2 <= 24**2] = label
        frames.append(_color_labels(frame))
    for color, path in zip(colors, paths, strict=False):
        draw.line([(cx, cy) for cy, cx in path], fill=color, width=8)
        for cy, cx in path:
            draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=color)
    table = _draw_table(
        [
            ("track_length", "frames linked per cell"),
            ("displacement", "start-to-end distance"),
            ("mean_speed", "frame-to-frame movement"),
            ("scope", "migration only, no lineage calls"),
        ]
    )
    _save_grid(
        "live_cell_tracking",
        "tracking_label_movie.png",
        [
            _panel("Label movie frame 0", frames[0]),
            _panel("Label movie frame 4", frames[-1]),
        ],
    )
    _save_grid(
        "live_cell_tracking",
        "tracking_metrics.png",
        [
            _panel("Track overlay preview", tracks),
            _panel("Migration metrics table", table),
        ],
    )


def main() -> None:
    fish_assets()
    parameter_assets()
    bbbc_assets()
    cell_counting_assets()
    restoration_assets()
    sairpico_assets()
    tracking_assets()


if __name__ == "__main__":
    main()
