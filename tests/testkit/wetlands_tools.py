"""Shared ordinary module authority for the real Wetlands test tools."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_tools() -> ModuleType:
    source = (Path(__file__).parents[1] / "integration" / "wetlands_test_tools.py").resolve(strict=True)
    name = source.stem
    resident = sys.modules.get(name)
    if resident is not None:
        resident_source = getattr(resident, "__file__", None)
        if not isinstance(resident_source, str) or Path(resident_source).resolve() != source:
            raise RuntimeError(f"Conflicting test-tool module owner: {name}")
        return resident
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Wetlands test tools: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(name) is module:
            del sys.modules[name]
        raise
    return module


_tools = _load_tools()
BatchTool = _tools.BatchTool
CancellableBatchTool = _tools.CancellableBatchTool
CancellableRowTool = _tools.CancellableRowTool
ErrorRowTool = _tools.ErrorRowTool
GpuTool = _tools.GpuTool
ProgressReportingTool = _tools.ProgressReportingTool
SimpleRowTool = _tools.SimpleRowTool
SlowRowTool = _tools.SlowRowTool
WorkerStreamTool = _tools.WorkerStreamTool

__all__ = [
    "BatchTool",
    "CancellableBatchTool",
    "CancellableRowTool",
    "ErrorRowTool",
    "GpuTool",
    "ProgressReportingTool",
    "SimpleRowTool",
    "SlowRowTool",
    "WorkerStreamTool",
]
