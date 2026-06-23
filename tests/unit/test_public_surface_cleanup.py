from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

TEXT_SUFFIXES = {".py", ".md", ".rst", ".yml", ".yaml", ".toml"}
SCAN_ROOTS = [
    ROOT / "docs",
    ROOT / "example_workflows",
    ROOT / "packages",
    ROOT / "tests",
]
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "_build",
}


def _public_text_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in TEXT_SUFFIXES:
                continue
            if EXCLUDED_PARTS & set(path.parts):
                continue
            if path.name.startswith("progress_log_"):
                continue
            files.append(path)
    return files


def test_public_surface_has_no_retired_tooling_or_test_language() -> None:
    banned = [
        "from bioimageflow_io_tools import " + "ReadImage",
        "Read" + "Image()",
        "tools/" + "read_image",
        "Synthetic" + "BBBC038Benchmark",
        'runtime="' + 'deterministic"',
        "deterministic " + "runtime",
        "smoke " + "workflow",
        "Analysis " + "question",
        "backend=" + '"baseline"',
    ]
    offenders: list[str] = []
    for path in _public_text_files():
        text = path.read_text(errors="ignore")
        for needle in banned:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")

    assert offenders == []


def test_retired_workflow_and_docs_directories_are_absent() -> None:
    retired = [
        ROOT / ("docs/source/" + "priority" + "_workflows"),
        ROOT / ("docs/source/" + "specialized_tool" + "_workflows"),
        ROOT / ("example_workflows/" + "cellpose3" + "_stardist"),
        ROOT / ("example_workflows/" + "fish_analysis" + "_sub_workflows"),
        ROOT / ("example_workflows/" + "ome" + "_normalization"),
        ROOT / ("example_workflows/" + "puncta" + "_analysis"),
        ROOT / ("example_workflows/" + "restoration" + "_benchmark"),
        ROOT / ("example_workflows/" + "sairpico_restoration" + "_smoke"),
        ROOT / ("example_workflows/" + "tracking" + "_analysis"),
    ]

    assert [path.relative_to(ROOT) for path in retired if path.exists()] == []
