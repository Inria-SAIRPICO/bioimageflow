"""Files — source DataFrameTool for explicit files or directory scans."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import Category, Connectable, GUIMeta, IOModel, PathPicker
from bioimageflow import DataFrameTool


class Files(DataFrameTool):
    """Create a source DataFrame from explicit files or a directory scan."""
    display_name = "Files"
    documentation = (
        "List explicit files or scan a directory with an optional glob pattern. "
        "Directory and Files are mutually exclusive; pattern and recursive apply "
        "only to Directory. "
        "Produces a DataFrame with a 'path' column."
    )
    category = Category.UTILITIES
    tags = ["source", "loader"]
    accepts_upstream = False

    class Inputs(IOModel):
        path: Annotated[Path | None, GUIMeta(
            display_name="Directory",
            description=(
                "Directory to scan. Mutually exclusive with Files; Glob pattern "
                "and Recursive apply only to this source."
            ),
            connectable=Connectable.NEVER,
            path_picker=PathPicker.FOLDER,
        )] = None
        files: Annotated[list[Path] | None, GUIMeta(
            display_name="Files",
            description=(
                "Explicit files in output order. Mutually exclusive with Directory; "
                "Glob pattern and Recursive are ignored."
            ),
            connectable=Connectable.NEVER,
        )] = None
        pattern: Annotated[str, GUIMeta(
            display_name="Glob pattern",
            description=(
                "Directory-only glob pattern (e.g. '*.tif', '*.png'). "
                "Ignored when Files is used."
            ),
            connectable=Connectable.NEVER,
        )] = "*"
        recursive: Annotated[bool, GUIMeta(
            display_name="Recursive",
            description=(
                "Scan matching files in descendant directories. "
                "Ignored when Files is used."
            ),
            connectable=Connectable.NEVER,
        )] = False

    class Outputs(IOModel):
        path: Annotated[Path, GUIMeta(
            display_name="Path",
            description="Absolute path of a matching file.",
        )]

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd

        directory_value = getattr(arguments, "path", None)
        explicit_values = getattr(arguments, "files", None)
        explicit_files = list(explicit_values or [])
        has_directory = directory_value is not None
        has_explicit_files = bool(explicit_files)

        if has_directory and has_explicit_files:
            raise ValueError("Set either Directory or Files, not both.")
        if not has_directory and not has_explicit_files:
            raise ValueError("Set a Directory or at least one file.")

        if has_explicit_files:
            files = [Path(value) for value in explicit_files]
            invalid = [path for path in files if not path.is_file()]
            if invalid:
                paths = ", ".join(str(path) for path in invalid)
                raise ValueError(f"Files contains missing or non-file paths: {paths}")
        else:
            directory = Path(directory_value)
            if not directory.is_dir():
                raise ValueError(f"Directory is missing or not a directory: {directory}")
            pattern = getattr(arguments, "pattern", "*")
            recursive = bool(getattr(arguments, "recursive", False))
            candidates = directory.rglob(pattern) if recursive else directory.glob(pattern)
            files = sorted(path for path in candidates if path.is_file())

        rows = [{"path": str(path)} for path in files]
        return pd.DataFrame(rows)
