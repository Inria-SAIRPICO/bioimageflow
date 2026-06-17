"""Legacy timestamp/hash directory helpers for workflow storage."""

from pathlib import Path


def get_node_dir(storage_path: str | Path, node_name: str) -> Path:
    """Get the directory for a node's data."""
    return Path(storage_path) / "data" / node_name


def has_other_hash_dirs(node_dir: str | Path, sig_hash: str) -> bool:
    """Return True if *node_dir* contains hash sub-directories whose
    short suffix differs from ``sig_hash[:12]``.

    Used by :meth:`Workflow.plan` to distinguish "prior_selection_miss" (the
    node was run before with different parameters) from "unexecuted" (no
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
