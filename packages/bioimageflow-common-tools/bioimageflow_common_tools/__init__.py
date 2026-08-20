"""BioImageFlow Common Tools — reusable bioimage analysis tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .connected_components import ConnectedComponents as ConnectedComponents
    from .files import Files as Files
    from .generate import Generate as Generate
    from .label_overlaps import LabelOverlaps as LabelOverlaps
    from .merge import Collect as Collect
    from .merge import Concat as Concat
    from .merge import CrossJoin as CrossJoin
    from .merge import InnerJoin as InnerJoin
    from .merge import JoinOnColumn as JoinOnColumn
    from .mosaic import Mosaic as Mosaic
    from .table import FilterTableRows as FilterTableRows
    from .table import SelectColumns as SelectColumns
    from .table import TableFromCsv as TableFromCsv
    from .table import WriteTable as WriteTable


_EXPORTS = {
    "Collect": ("merge", "Collect"),
    "Concat": ("merge", "Concat"),
    "ConnectedComponents": ("connected_components", "ConnectedComponents"),
    "CrossJoin": ("merge", "CrossJoin"),
    "Files": ("files", "Files"),
    "FilterTableRows": ("table", "FilterTableRows"),
    "Generate": ("generate", "Generate"),
    "InnerJoin": ("merge", "InnerJoin"),
    "JoinOnColumn": ("merge", "JoinOnColumn"),
    "LabelOverlaps": ("label_overlaps", "LabelOverlaps"),
    "Mosaic": ("mosaic", "Mosaic"),
    "SelectColumns": ("table", "SelectColumns"),
    "TableFromCsv": ("table", "TableFromCsv"),
    "WriteTable": ("table", "WriteTable"),
}

__all__ = [
    "Collect",
    "Concat",
    "ConnectedComponents",
    "CrossJoin",
    "Files",
    "FilterTableRows",
    "Generate",
    "InnerJoin",
    "JoinOnColumn",
    "LabelOverlaps",
    "Mosaic",
    "SelectColumns",
    "TableFromCsv",
    "WriteTable",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value
