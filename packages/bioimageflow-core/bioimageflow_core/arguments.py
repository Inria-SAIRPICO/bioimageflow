"""Arguments namespace and index lineage helpers."""

from difflib import get_close_matches as _get_close_matches
from typing import Any


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
