from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import imageio.v3 as iio
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = Path(os.environ.get("BIOIMAGEFLOW_DOC_ASSET_SOURCE", ROOT)).resolve()
EXAMPLE_WORKFLOWS = SOURCE_ROOT / "example_workflows"
OUTPUTS_ROOT = Path(
    os.environ.get("BIOIMAGEFLOW_DOC_WORKFLOW_OUTPUTS", EXAMPLE_WORKFLOWS / "outputs")
).resolve()
OUT = Path(__file__).resolve().parent


class WorkflowArtifactError(RuntimeError):
    """Raised when a required real workflow artifact is unavailable."""


def _font(size: int = 18) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("Arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _read_image(path: Path) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"Required image does not exist: {path}")
    return np.asarray(iio.imread(path))


def _require_path(path: Path, label: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Required {label} does not exist: {path}")
    return path


def _normalize(image: np.ndarray) -> np.ndarray:
    data = np.asarray(image, dtype=float)
    data = np.squeeze(data)
    if data.ndim > 2:
        data = data[..., 0] if data.shape[-1] in {3, 4} else data[0]
    low, high = np.percentile(data, [1, 99.8])
    if high <= low:
        high = float(data.max() or 1)
        low = float(data.min())
    scaled = np.clip((data - low) / (high - low), 0, 1)
    return (scaled * 255).astype(np.uint8)


def _as_gray(image: np.ndarray) -> np.ndarray:
    data = np.asarray(image)
    data = np.squeeze(data)
    if data.ndim == 2:
        return data
    if data.ndim == 3 and data.shape[-1] in {3, 4}:
        return data[..., :3].mean(axis=-1)
    if data.ndim == 3:
        return data.max(axis=0)
    raise ValueError(f"Expected a 2D or 3D image, got shape {data.shape}.")


def _center_crop(image: np.ndarray, size: int = 360) -> np.ndarray:
    image = np.asarray(image)
    if image.ndim == 3 and image.shape[0] not in {3, 4} and image.shape[-1] not in {3, 4}:
        image = image[0]
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
    image = np.asarray(image)
    if image.ndim != 3:
        return np.repeat(_normalize(image)[..., None], 3, axis=-1)
    if image.shape[0] >= 3:
        ch0 = _normalize(image[0])
        ch1 = _normalize(image[1])
        ch2 = _normalize(image[2])
        return np.stack([ch1, ch0, ch2], axis=-1)
    return np.repeat(_normalize(image[0])[..., None], 3, axis=-1)


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
            [70, 190, 190],
            [230, 120, 180],
        ],
        dtype=np.uint8,
    )
    return palette[labels.astype(np.uint64) % len(palette)]


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


def _label_spot_overlay(labels: np.ndarray, fols2_spots: np.ndarray, csf1r_spots: np.ndarray) -> np.ndarray:
    labels = np.asarray(labels)
    colored = _color_labels(labels)
    background = np.full_like(colored, [14, 16, 22])
    overlay = np.where((labels > 0)[..., None], colored, background)
    overlay[_dilate_mask(fols2_spots, radius=2)] = [60, 255, 120]
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


def _resolve_link(link_path: Path, *, kind: str | None = None) -> Path:
    payload = json.loads(link_path.read_text())
    if payload.get("schema") != "bioimageflow.link.v1":
        raise WorkflowArtifactError(f"Invalid BioImageFlow link schema: {link_path}")
    if kind is not None and payload.get("kind") != kind:
        raise WorkflowArtifactError(f"Expected {kind} link at {link_path}, got {payload.get('kind')!r}.")
    target = payload.get("target")
    if not isinstance(target, str) or not target:
        raise WorkflowArtifactError(f"BioImageFlow link has no target: {link_path}")
    target_path = Path(target)
    return target_path if target_path.is_absolute() else (link_path.parent / target_path).resolve()


class WorkflowArtifacts:
    def __init__(self, workflow: str) -> None:
        self.workflow = workflow
        self.storage_root = self._find_storage_root()

    def _find_storage_root(self) -> Path:
        candidates = [OUTPUTS_ROOT / self.workflow, OUTPUTS_ROOT / self.workflow / "bif"]
        for candidate in candidates:
            if (candidate / "views").exists() or (candidate / "cache" / "v1").exists():
                return candidate
        raise WorkflowArtifactError(
            f"No workflow storage found for {self.workflow!r}. Expected one of: "
            + ", ".join(str(path) for path in candidates)
        )

    @property
    def runs_root(self) -> Path:
        return self.storage_root / "views" / "runs"

    @property
    def latest_root(self) -> Path:
        return self.storage_root / "views" / "latest"

    def latest_run_dir(self) -> Path:
        latest = self.runs_root / "latest-success.bioimageflow-link.json"
        if latest.exists():
            return _resolve_link(latest, kind="directory")
        runs = [
            path
            for path in sorted(self.runs_root.glob("run_*"))
            if (path / "run.json").exists()
            and json.loads((path / "run.json").read_text()).get("status") == "succeeded"
        ]
        if not runs:
            raise WorkflowArtifactError(f"No successful run view found for {self.workflow}.")
        return runs[-1]

    def latest_node_dir(self, node_key: str) -> Path:
        latest_link = self.latest_root.joinpath(*node_key.split("/")).with_suffix(".bioimageflow-link.json")
        if latest_link.exists():
            return _resolve_link(latest_link, kind="directory")
        node_dir = self.latest_run_dir() / "nodes" / node_key
        if (node_dir / "result.json").exists():
            return node_dir
        raise WorkflowArtifactError(f"No latest run view found for node {self.workflow}/{node_key}.")

    def node_keys(self) -> list[str]:
        run_nodes = self.latest_run_dir() / "nodes"
        if not run_nodes.exists():
            return []
        return sorted(
            str(path.relative_to(run_nodes).parent).replace(os.sep, "/")
            for path in run_nodes.rglob("result.json")
        )

    def record_dir(self, node_key: str) -> Path:
        node_dir = self.latest_node_dir(node_key)
        result_path = node_dir / "result.json"
        payload = json.loads(result_path.read_text())
        canonical = payload.get("canonical")
        if isinstance(canonical, str) and canonical:
            return (result_path.parent / canonical).resolve()
        record_link = node_dir / "record.bioimageflow-link.json"
        if record_link.exists():
            return _resolve_link(record_link, kind="directory")
        raise WorkflowArtifactError(f"Node result has no canonical record: {self.workflow}/{node_key}")

    def dataframe(self, node_key: str) -> pd.DataFrame:
        record_dir = self.record_dir(node_key)
        manifest_path = record_dir / "manifest.json"
        dataframe_path = record_dir / "dataframe.parquet"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            dataframe = manifest.get("dataframe")
            if isinstance(dataframe, dict) and isinstance(dataframe.get("path"), str):
                dataframe_path = record_dir / dataframe["path"]
        if dataframe_path.exists():
            if dataframe_path.suffix == ".csv":
                return pd.read_csv(dataframe_path)
            return pd.read_parquet(dataframe_path)
        csv_path = record_dir / "dataframe.csv"
        if csv_path.exists():
            return pd.read_csv(csv_path)
        raise WorkflowArtifactError(f"No dataframe found for {self.workflow}/{node_key} in {record_dir}.")

    def node_with_columns(
        self,
        node_candidates: list[str],
        columns: set[str],
        *,
        prefixes: tuple[str, ...] = (),
        name_contains: tuple[str, ...] = (),
    ) -> str:
        for node_key in node_candidates:
            try:
                df = self.dataframe(node_key)
            except (FileNotFoundError, WorkflowArtifactError):
                continue
            if columns.issubset(df.columns):
                return node_key
        for node_key in self.node_keys():
            if node_key in node_candidates:
                continue
            if prefixes and not node_key.startswith(prefixes):
                continue
            if name_contains and not any(fragment in node_key for fragment in name_contains):
                continue
            try:
                df = self.dataframe(node_key)
            except (FileNotFoundError, WorkflowArtifactError):
                continue
            if columns.issubset(df.columns):
                return node_key
        raise WorkflowArtifactError(
            f"No node in {self.workflow} exposes dataframe columns {sorted(columns)}."
        )

    def path_from_column(self, node_key: str, column: str, row: int = 0) -> Path:
        record_dir = self.record_dir(node_key)
        df = self.dataframe(node_key)
        if column not in df.columns:
            raise WorkflowArtifactError(f"{self.workflow}/{node_key} has no column {column!r}.")
        if len(df) <= row:
            raise WorkflowArtifactError(f"{self.workflow}/{node_key} has no row {row}.")
        value = df[column].iloc[row]
        if pd.isna(value):
            raise WorkflowArtifactError(f"{self.workflow}/{node_key}.{column} row {row} is null.")
        return self._resolve_record_path(record_dir, Path(str(value)))

    def paths_from_column(self, node_key: str, column: str) -> list[Path]:
        record_dir = self.record_dir(node_key)
        df = self.dataframe(node_key)
        if column not in df.columns:
            raise WorkflowArtifactError(f"{self.workflow}/{node_key} has no column {column!r}.")
        paths = [self._resolve_record_path(record_dir, Path(str(value))) for value in df[column].dropna()]
        if not paths:
            raise WorkflowArtifactError(f"{self.workflow}/{node_key}.{column} has no path values.")
        return paths

    def _resolve_record_path(self, record_dir: Path, value: Path) -> Path:
        candidates: list[Path]
        if value.is_absolute():
            candidates = [value]
        else:
            candidates = [record_dir / value, self.storage_root / value, SOURCE_ROOT / value, ROOT / value]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        raise FileNotFoundError(
            f"Path-valued workflow output does not exist for {self.workflow}: {value}"
        )


def _real_fish_image() -> Path:
    return _require_path(
        EXAMPLE_WORKFLOWS / "fish_analysis" / "data" / "13432.tif",
        "FISH CIL 13432 input image",
    )


def _bbbc_sample_root() -> Path:
    stage_dir = EXAMPLE_WORKFLOWS / "bbbc038_segmentation_benchmark" / "data" / "stage1_train"
    samples = sorted(path for path in stage_dir.glob("*") if path.is_dir())
    if not samples:
        raise FileNotFoundError(f"No BBBC038 samples found in {stage_dir}")
    return samples[0]


def _bbbc_input_image() -> Path:
    sample = _bbbc_sample_root()
    images = sorted((sample / "images").glob("*"))
    if not images:
        raise FileNotFoundError(f"No BBBC038 input image found in {sample / 'images'}")
    return images[0]


def _bbbc_reference_labels() -> np.ndarray:
    artifacts = WorkflowArtifacts("bbbc038_segmentation_benchmark")
    return _read_image(artifacts.path_from_column("build_reference_labels", "reference_label_image"))


def _format_number(value: Any, digits: int = 3) -> str:
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.{digits}g}"
    return str(value)


def fish_assets() -> None:
    image = _center_crop(_read_image(_real_fish_image()), 420)
    rgb = _rgb_from_cyx(image)
    artifacts = WorkflowArtifacts("fish_analysis")
    nuclei = _center_crop(_read_image(artifacts.path_from_column("cellpose3_nuclei", "mask")), 420)
    fols2_node = artifacts.node_with_columns(
        [
            "fols2_marker_spot_analysis/AtlasSpotDetection_1",
            "fols2_marker_spot_analysis/ConnectedComponents_1",
        ],
        {"output_image"},
        prefixes=("fols2_marker_spot_analysis/",),
        name_contains=("AtlasSpotDetection", "ConnectedComponents"),
    )
    csf1r_node = artifacts.node_with_columns(
        [
            "csf1r_marker_spot_analysis/AtlasSpotDetection_1",
            "csf1r_marker_spot_analysis/ConnectedComponents_1",
        ],
        {"output_image"},
        prefixes=("csf1r_marker_spot_analysis/",),
        name_contains=("AtlasSpotDetection", "ConnectedComponents"),
    )
    fols2_spots = _center_crop(_read_image(artifacts.path_from_column(fols2_node, "output_image")), 420)
    csf1r_spots = _center_crop(_read_image(artifacts.path_from_column(csf1r_node, "output_image")), 420)
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
        [_label_spot_overlay(nuclei, fols2_spots, csf1r_spots)],
        columns=1,
        gap=0,
    )


def parameter_assets() -> None:
    raw = _center_crop(_read_image(_real_fish_image()), 420)
    marker = raw[0] if raw.ndim == 3 else raw
    artifacts = WorkflowArtifacts("parameter_space_exploration")
    detections_node = artifacts.node_with_columns(["atlas_detections"], {"output_image"}, name_contains=("atlas",))
    detection_paths = artifacts.paths_from_column(detections_node, "output_image")
    mosaic_path = artifacts.path_from_column("results_mosaic", "mosaic_path")
    _save_image_grid(
        "parameter_space_exploration",
        "atlas_input_and_mask.png",
        [
            _rgb_from_cyx(raw),
            _channel_color(marker, (0, 255, 90)),
        ],
    )
    detection_images = [_normalize(_center_crop(_read_image(path), 420)) for path in detection_paths]
    if len(detection_images) < 2:
        detection_images = [_normalize(_read_image(mosaic_path))]
        columns = 1
    else:
        columns = min(3, len(detection_images))
    _save_image_grid(
        "parameter_space_exploration",
        "parameter_results.png",
        detection_images,
        columns=columns,
        gap=0,
    )


def bbbc_assets() -> None:
    image = _center_crop(_as_gray(_read_image(_bbbc_input_image())), 420)
    reference = _center_crop(_bbbc_reference_labels(), 420)
    _save_image_grid(
        "bbbc038_segmentation_benchmark",
        "bbbc038_input_reference.png",
        [
            _normalize(image),
            _color_labels(reference),
        ],
    )
    artifacts = WorkflowArtifacts("bbbc038_segmentation_benchmark")
    overlays: list[np.ndarray] = []
    for node_key in [
        "benchmark_cellpose3",
        "benchmark_cellpose_sam",
        "benchmark_stardist",
        "benchmark_classical_threshold",
    ]:
        overlays.append(_read_image(artifacts.path_from_column(node_key, "overlay_image")))
    _save_image_grid(
        "bbbc038_segmentation_benchmark",
        "bbbc038_method_overlays.png",
        overlays,
        columns=2,
        gap=8,
    )


def cell_counting_assets() -> None:
    image = _center_crop(_as_gray(_read_image(_bbbc_input_image())), 420)
    artifacts = WorkflowArtifacts("cell_counting_phenotyping")
    labels = _center_crop(_read_image(artifacts.path_from_column("segment_cells", "labels")), 420)
    summary = artifacts.dataframe("summarize_phenotypes")
    if summary.empty:
        raise WorkflowArtifactError("cell_counting_phenotyping/summarize_phenotypes is empty.")
    row = summary.iloc[0]
    table = _draw_table(
        [
            ("object_count", _format_number(row["object_count"], 0)),
            ("mean_area", _format_number(row["mean_area"])),
            ("mean_intensity", _format_number(row["mean_intensity"])),
            ("mean_perimeter", _format_number(row["mean_perimeter"])),
        ]
    )
    _save_grid(
        "cell_counting_phenotyping",
        "cell_counting_input_labels.png",
        [
            _panel("Microscopy crop for counting", _normalize(image)),
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
    artifacts = WorkflowArtifacts("low_snr_restoration")
    results = artifacts.dataframe("restoration_results")
    if results.empty:
        raise WorkflowArtifactError("low_snr_restoration/restoration_results is empty.")
    row = results.iloc[0]
    record_dir = artifacts.record_dir("restoration_results")
    clean = _center_crop(_normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["clean_image"])))), 420)
    degraded = _center_crop(_normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["degraded_image"])))), 420)
    restored = _center_crop(_normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["restored_image"])))), 420)
    table = _draw_table(
        [
            ("mse_degraded", _format_number(row["mse_degraded"])),
            ("mse_restored", _format_number(row["mse_restored"])),
            ("degraded_psnr", _format_number(row["degraded_psnr"])),
            ("restored_psnr", _format_number(row["restored_psnr"])),
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
    artifacts = WorkflowArtifacts("sairpico_deconvolution")
    metrics = artifacts.dataframe("sairpico_deconvolution_metrics")
    if metrics.empty:
        raise WorkflowArtifactError("sairpico_deconvolution/sairpico_deconvolution_metrics is empty.")
    row = metrics.iloc[0]
    record_dir = artifacts.record_dir("sairpico_deconvolution_metrics")
    input_image = _center_crop(_normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["input_image"])))), 420)
    psf = _normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["psf_image"]))))
    denoised = _center_crop(_normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["denoised_image"])))), 420)
    deconvolved = _center_crop(_normalize(_read_image(artifacts._resolve_record_path(record_dir, Path(row["deconvolved_image"])))), 420)
    _save_grid(
        "sairpico_deconvolution",
        "sairpico_input_psf.png",
        [
            _panel("Microscopy crop supplied to SAIRPICO", input_image),
            _panel("Workflow-generated Gaussian PSF", psf),
        ],
    )
    _save_grid(
        "sairpico_deconvolution",
        "sairpico_outputs.png",
        [
            _panel("Denoised image", denoised),
            _panel("Richardson-Lucy deconvolution", deconvolved),
        ],
    )


def _tracking_label_movie_path() -> Path:
    candidates = [
        EXAMPLE_WORKFLOWS / "live_cell_tracking" / "data" / "ctc_label_movie.tif",
        EXAMPLE_WORKFLOWS / "live_cell_tracking" / "ctc_label_movie.tif",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "No real live-cell tracking label movie found. Expected ctc_label_movie.tif "
        "under example_workflows/live_cell_tracking/data/."
    )


def _crop_offsets(shape: tuple[int, int], size: int = 420) -> tuple[int, int]:
    height, width = shape
    return max((height - size) // 2, 0), max((width - size) // 2, 0)


def tracking_assets() -> None:
    labels = np.asarray(_read_image(_tracking_label_movie_path()))
    if labels.ndim == 2:
        labels = labels[np.newaxis, ...]
    if labels.ndim != 3:
        raise ValueError(f"Expected TYX tracking labels, got shape {labels.shape}.")
    frame_indices = sorted({0, min(2, labels.shape[0] - 1), labels.shape[0] - 1})
    frames = [_color_labels(_center_crop(labels[index], 420)) for index in frame_indices]
    artifacts = WorkflowArtifacts("live_cell_tracking")
    metrics = artifacts.dataframe("migration_metrics")
    if metrics.empty:
        raise WorkflowArtifactError("live_cell_tracking/migration_metrics is empty.")
    tracks_df = artifacts.dataframe("nearest_neighbor_tracks")
    required_track_columns = {"track_id", "frame", "y", "x"}
    if not required_track_columns.issubset(tracks_df.columns):
        raise WorkflowArtifactError(
            "live_cell_tracking/nearest_neighbor_tracks is missing real track centroid columns: "
            + ", ".join(sorted(required_track_columns - set(tracks_df.columns)))
        )
    y0, x0 = _crop_offsets(labels.shape[-2:], 420)
    tracks = Image.new("RGB", (420, 420), "white")
    draw = ImageDraw.Draw(tracks)
    colors = [(230, 80, 80), (55, 130, 220), (80, 170, 110), (180, 110, 220)]
    for index, (track_id, table) in enumerate(tracks_df.groupby("track_id")):
        points = []
        for _, point in table.sort_values("frame").iterrows():
            x = int(round(float(point["x"]) - x0))
            y = int(round(float(point["y"]) - y0))
            if 0 <= x < 420 and 0 <= y < 420:
                points.append((x, y))
        if len(points) >= 2:
            draw.line(points, fill=colors[index % len(colors)], width=6)
        for x, y in points:
            draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=colors[index % len(colors)])
        if points:
            draw.text(points[-1], str(track_id), fill=(30, 40, 50), font=_font(14))
    row = metrics.iloc[0]
    table = _draw_table(
        [
            ("track_count", _format_number(row["track_count"], 0)),
            ("mean_track_length", _format_number(row["mean_track_length"])),
            ("net_displacement", _format_number(row["net_displacement"])),
            ("net_speed", _format_number(row["net_speed"])),
            ("gap_count", _format_number(row["gap_count"], 0)),
            ("short_track_fraction", _format_number(row["short_track_fraction"])),
        ]
    )
    _save_grid(
        "live_cell_tracking",
        "tracking_label_movie.png",
        [
            _panel(f"Label movie frame {frame_indices[0]}", frames[0]),
            _panel(f"Label movie frame {frame_indices[-1]}", frames[-1]),
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
