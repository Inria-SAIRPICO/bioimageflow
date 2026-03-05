"""File management for workflow storage."""

from pathlib import Path


def get_node_dir(storage_path: str | Path, node_name: str) -> Path:
    """Get the directory for a node's data."""
    return Path(storage_path) / "data" / node_name


def get_hash_dir(node_dir: str | Path, sig_hash: str) -> Path:
    """Get the directory for a specific hash execution."""
    return Path(node_dir) / sig_hash


def get_assets_dir(hash_dir: str | Path) -> Path:
    """Get the assets directory within a hash dir."""
    return Path(hash_dir) / "assets"


def ensure_dirs(hash_dir: str | Path) -> None:
    """Create all directories needed for a hash execution."""
    hash_dir = Path(hash_dir)
    hash_dir.mkdir(parents=True, exist_ok=True)
    (hash_dir / "assets").mkdir(exist_ok=True)
