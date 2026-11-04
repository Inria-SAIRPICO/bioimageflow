"""Arguments namespace, execution context, and index lineage helpers."""

from dataclasses import dataclass, field
from collections.abc import Mapping
from difflib import get_close_matches as _get_close_matches
from pathlib import Path
import math
from typing import Any, Optional


@dataclass(frozen=True)
class ReferenceRow:
    """One actual auxiliary record, separate from consumed observation rows."""

    position: int
    row_index: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class PlannedFile:
    """An admitted exact destination and its actual contributing rows."""

    path: Path
    group_index: int
    key_values: tuple[tuple[str, Any], ...] = ()
    row_positions: tuple[int, ...] = ()
    reference_positions: tuple[int, ...] = ()

    @property
    def key(self) -> dict[str, Any]:
        return dict(self.key_values)

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "group_index": self.group_index,
                "key": [[name, str(value) if isinstance(value, Path) else value,
                         "path" if isinstance(value, Path) else "value"] for name, value in self.key_values],
                "row_positions": list(self.row_positions),
                "reference_positions": list(self.reference_positions)}

    @classmethod
    def from_dict(cls, value: Any, assets_dir: Path) -> "PlannedFile":
        if type(value) is not dict or set(value) != {"path", "group_index", "key", "row_positions", "reference_positions"}:
            raise ValueError("Planned file has invalid fields")
        path = Path(value["path"])
        if type(value["path"]) is not str or str(path) != value["path"] or not path.is_absolute() or not path.is_relative_to(assets_dir):
            raise ValueError("Planned file must be inside invocation assets")
        if any(part in ("..", ".") for part in path.parts):
            raise ValueError("Planned file must be normalized")
        if not path.resolve().is_relative_to(assets_dir.resolve()):
            raise ValueError("Planned file escapes invocation assets")
        index = value["group_index"]
        if type(index) is not int or index < 0:
            raise ValueError("Invalid file group index")
        positions = []
        for name in ("row_positions", "reference_positions"):
            items = value[name]
            if type(items) is not list or any(type(item) is not int or item < 0 for item in items) or len(set(items)) != len(items):
                raise ValueError("Invalid planned-file member positions")
            positions.append(tuple(items))
        keys = []
        if type(value["key"]) is not list:
            raise ValueError("Invalid planned-file key")
        for item in value["key"]:
            if type(item) is not list or len(item) != 3 or type(item[0]) is not str or item[2] not in ("value", "path"):
                raise ValueError("Invalid planned-file key member")
            member = item[1]
            if member is not None and type(member) not in (str, int, float, bool):
                raise ValueError("Planned-file key must be scalar")
            if type(member) is float and not math.isfinite(member):
                raise ValueError("Planned-file keys must be finite")
            if item[2] == "path":
                if type(member) is not str:
                    raise ValueError("Invalid path key")
                member = Path(member)
            keys.append((item[0], member))
        if len({name for name, _ in keys}) != len(keys):
            raise ValueError("Duplicate file group keys")
        return cls(path, index, tuple(keys), *positions)


@dataclass(frozen=True, eq=False)
class _OutputFiles(Mapping):
    """Picklable immutable projection; callbacks cannot change planner authority."""

    entries: tuple[tuple[str, tuple[PlannedFile, ...]], ...]

    def __getitem__(self, key: str) -> tuple[PlannedFile, ...]:
        for name, values in self.entries:
            if name == key:
                return values
        raise KeyError(key)

    def __iter__(self):
        return (name for name, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ExecutionContext:
    """Per-execution filesystem context for ProcessingTool runtime scratch."""

    run_dir: Path
    assets_dir: Path
    work_dir: Path
    rows_dir: Path
    row_dir: Optional[Path] = None
    batch_dir: Optional[Path] = None
    row_index: Optional[str] = None
    batch_arguments: Optional["Arguments"] = None
    reference_rows: tuple[ReferenceRow, ...] = ()
    output_files: Mapping[str, tuple[PlannedFile, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_files", _OutputFiles(tuple(
            (name, tuple(items)) for name, items in self.output_files.items()
        )))
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

    def to_dict(self) -> dict[str, Any]:
        """Return a picklable representation for worker dispatch."""
        return {
            "run_dir": str(self.run_dir),
            "assets_dir": str(self.assets_dir),
            "work_dir": str(self.work_dir),
            "rows_dir": str(self.rows_dir),
            "row_dir": str(self.row_dir) if self.row_dir is not None else None,
            "batch_dir": str(self.batch_dir) if self.batch_dir is not None else None,
            "row_index": self.row_index,
            "output_files": {name: [item.to_dict() for item in items] for name, items in self.output_files.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionContext":
        """Reconstruct an execution context from a worker payload."""
        files = data["output_files"]
        if type(files) is not dict or any(type(name) is not str or type(items) is not list for name, items in files.items()):
            raise ValueError("Invalid output-files projection")
        return cls(
            run_dir=Path(data["run_dir"]),
            assets_dir=Path(data["assets_dir"]),
            work_dir=Path(data["work_dir"]),
            rows_dir=Path(data["rows_dir"]),
            row_dir=Path(data["row_dir"]) if data["row_dir"] is not None else None,
            batch_dir=Path(data["batch_dir"]) if data["batch_dir"] is not None else None,
            row_index=data.get("row_index"),
            output_files={name: tuple(PlannedFile.from_dict(item, Path(data["assets_dir"])) for item in items)
                          for name, items in files.items()},
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
