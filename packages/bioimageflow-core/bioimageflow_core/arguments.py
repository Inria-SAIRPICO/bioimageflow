"""Arguments namespace, execution context, and index lineage helpers."""

from dataclasses import dataclass
from difflib import get_close_matches as _get_close_matches
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExecutionContext:
    """Per-execution filesystem context for ProcessingTool runtime scratch."""

    run_dir: Path
    assets_dir: Path
    work_dir: Path
    rows_dir: Path
    row_dir: Path | None = None
    batch_dir: Path | None = None
    row_index: str | None = None

    def __post_init__(self) -> None:
        expected_work_dir = self.run_dir / "work"
        if self.work_dir != expected_work_dir:
            raise ValueError(
                f"ExecutionContext.work_dir must be {expected_work_dir}, "
                f"got {self.work_dir}."
            )

        expected_rows_dir = self.work_dir / "rows"
        if self.rows_dir != expected_rows_dir:
            raise ValueError(
                f"ExecutionContext.rows_dir must be {expected_rows_dir}, "
                f"got {self.rows_dir}."
            )

        if (self.row_dir is None) == (self.batch_dir is None):
            raise ValueError("ExecutionContext requires exactly one of row_dir or batch_dir.")

        if self.row_dir is not None:
            if self.row_dir.parent != self.rows_dir:
                raise ValueError(
                    f"ExecutionContext.row_dir must be a child of {self.rows_dir}, "
                    f"got {self.row_dir}."
                )
            if self.row_index is None:
                raise ValueError("ExecutionContext.row_index is required when row_dir is set.")

        if self.batch_dir is not None:
            expected_batch_dir = self.work_dir / "batch"
            if self.batch_dir != expected_batch_dir:
                raise ValueError(
                    f"ExecutionContext.batch_dir must be {expected_batch_dir}, "
                    f"got {self.batch_dir}."
                )
            if self.row_index is not None:
                raise ValueError("ExecutionContext.row_index must be None when batch_dir is set.")

    def to_dict(self) -> dict[str, str | None]:
        """Return a picklable representation for worker dispatch."""
        return {
            "run_dir": str(self.run_dir),
            "assets_dir": str(self.assets_dir),
            "work_dir": str(self.work_dir),
            "rows_dir": str(self.rows_dir),
            "row_dir": str(self.row_dir) if self.row_dir is not None else None,
            "batch_dir": str(self.batch_dir) if self.batch_dir is not None else None,
            "row_index": self.row_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionContext":
        """Reconstruct an execution context from a worker payload."""
        return cls(
            run_dir=Path(data["run_dir"]),
            assets_dir=Path(data["assets_dir"]),
            work_dir=Path(data["work_dir"]),
            rows_dir=Path(data["rows_dir"]),
            row_dir=Path(data["row_dir"]) if data["row_dir"] is not None else None,
            batch_dir=Path(data["batch_dir"]) if data["batch_dir"] is not None else None,
            row_index=data.get("row_index"),
        )


class Arguments:
    """
    Lightweight namespace for passing resolved values to tool methods.
    Supports attribute access with helpful error messages on typos.
    """
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith('_'):
            raise AttributeError(name)
        available = [k for k in self.__dict__ if not k.startswith('_')]
        close = _get_close_matches(name, available, n=3, cutoff=0.6)
        msg = f"Arguments has no field '{name}'."
        if close:
            msg += f" Did you mean: {', '.join(close)}?"
        else:
            msg += f" Available fields: {', '.join(sorted(available))}"
        raise AttributeError(msg)


def parse_index_lineage(index: str) -> list[str]:
    """Split an exploded index into its lineage components."""
    return index.split("::")


def parent_index(index: str) -> str:
    """Return the parent index (strip last explosion level)."""
    parts = index.split("::")
    return "::".join(parts[:-1]) if len(parts) > 1 else index
