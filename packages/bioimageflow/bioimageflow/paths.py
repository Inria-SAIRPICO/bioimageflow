"""Centralized path resolution for BioImageFlow state directories.

Resolution order (highest priority first):

1. Specific env var (``BIOIMAGEFLOW_TOOL_STORE`` / ``BIOIMAGEFLOW_WETLANDS``)
2. ``BIOIMAGEFLOW_HOME`` / ``<subdir>``
3. ``~/.bioimageflow/<subdir>``
"""

import os
from pathlib import Path

_DEFAULT_HOME = Path.home() / ".bioimageflow"


def get_home() -> Path:
    """Return the BioImageFlow home directory."""
    return Path(os.environ.get("BIOIMAGEFLOW_HOME", str(_DEFAULT_HOME)))


def get_tool_store_path() -> Path:
    """Return the tool store directory."""
    env = os.environ.get("BIOIMAGEFLOW_TOOL_STORE")
    if env:
        return Path(env)
    return get_home() / "tool_packages"


def get_wetlands_path() -> Path:
    """Return the default Wetlands instance directory."""
    env = os.environ.get("BIOIMAGEFLOW_WETLANDS")
    if env:
        return Path(env)
    return get_home() / "wetlands"
