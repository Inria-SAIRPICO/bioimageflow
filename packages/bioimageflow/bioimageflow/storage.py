"""File management for workflow storage."""

from datetime import datetime
from pathlib import Path


def get_node_dir(storage_path: str | Path, node_name: str) -> Path:
    """Get the directory for a node's data."""
    return Path(storage_path) / "data" / node_name


def get_hash_dir(node_dir: str | Path, sig_hash: str) -> Path:
    """Compose ``node_dir / sig_hash``.

    Used to build sentinel paths during planning (e.g. ``"pending"``
    for output asset templates rendered before execution). For real
    hash directories use :func:`find_hash_dir` (lookup) or
    :func:`create_hash_dir` (creation).
    """
    return Path(node_dir) / sig_hash


def find_hash_dir(node_dir: str | Path, sig_hash: str) -> Path | None:
    """Find an existing directory matching the short hash.

    Searches *node_dir* for a sub-directory whose name ends with
    ``_{sig_hash[:12]}``.  Returns the path if found, else ``None``.
    """
    node_dir = Path(node_dir)
    short = sig_hash[:12]
    if not node_dir.exists():
        return None
    for d in node_dir.iterdir():
        if d.is_dir() and d.name.endswith(f"_{short}"):
            return d
    return None


def has_other_hash_dirs(node_dir: str | Path, sig_hash: str) -> bool:
    """Return True if *node_dir* contains hash sub-directories whose
    short suffix differs from ``sig_hash[:12]``.

    Used by :meth:`Workflow.plan` to distinguish "out_of_date" (the node
    was run before with different parameters) from "unexecuted" (no
    storage at all).
    """
    node_dir = Path(node_dir)
    if not node_dir.exists():
        return False
    short = sig_hash[:12]
    for d in node_dir.iterdir():
        if not d.is_dir():
            continue
        # Hash sub-directories follow ``YYYYMMDD_HHMMSS_<short12>``.
        # Extract the trailing short hash; ignore directories that don't
        # match the convention.
        suffix = d.name.rsplit("_", 1)[-1]
        if len(suffix) == 12 and suffix != short:
            return True
    return False


def create_hash_dir(node_dir: str | Path, sig_hash: str) -> Path:
    """Create a new timestamped directory for *sig_hash*.

    The directory name has the format ``YYYYMMDD_HHMMSS_<hash[:12]>``.
    """
    node_dir = Path(node_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = sig_hash[:12]
    hash_dir = node_dir / f"{timestamp}_{short}"
    hash_dir.mkdir(parents=True, exist_ok=True)
    (hash_dir / "assets").mkdir(exist_ok=True)
    (hash_dir / "work").mkdir(exist_ok=True)
    return hash_dir


def get_assets_dir(hash_dir: str | Path) -> Path:
    """Get the assets directory within a hash dir."""
    return Path(hash_dir) / "assets"


def get_work_dir(hash_dir: str | Path) -> Path:
    """Get the scratch work directory within a hash dir."""
    return Path(hash_dir) / "work"


def get_rows_work_dir(hash_dir: str | Path) -> Path:
    """Get the parent directory for per-row scratch directories."""
    return get_work_dir(hash_dir) / "rows"


def get_batch_work_dir(hash_dir: str | Path) -> Path:
    """Get the scratch directory for process_batch execution."""
    return get_work_dir(hash_dir) / "batch"


def ensure_dirs(hash_dir: str | Path) -> None:
    """Create all directories needed for a hash execution."""
    hash_dir = Path(hash_dir)
    hash_dir.mkdir(parents=True, exist_ok=True)
    (hash_dir / "assets").mkdir(exist_ok=True)
    (hash_dir / "work").mkdir(exist_ok=True)
