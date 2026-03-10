"""File management for workflow storage."""

from datetime import datetime
from pathlib import Path


def get_node_dir(storage_path: str | Path, node_name: str) -> Path:
    """Get the directory for a node's data."""
    return Path(storage_path) / "data" / node_name


def get_hash_dir(node_dir: str | Path, sig_hash: str) -> Path:
    """Get the directory for a specific hash execution.

    .. deprecated:: Use :func:`find_hash_dir` or :func:`create_hash_dir` instead.
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
    return hash_dir


def get_assets_dir(hash_dir: str | Path) -> Path:
    """Get the assets directory within a hash dir."""
    return Path(hash_dir) / "assets"


def ensure_dirs(hash_dir: str | Path) -> None:
    """Create all directories needed for a hash execution."""
    hash_dir = Path(hash_dir)
    hash_dir.mkdir(parents=True, exist_ok=True)
    (hash_dir / "assets").mkdir(exist_ok=True)
