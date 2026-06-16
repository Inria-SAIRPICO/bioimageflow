"""Worker-side dispatcher for Wetlands environments.

This module is executed by Wetlands workers via ``env.submit()`` and
``env.map_tasks()``.  It discovers tool classes in a given module file
and dispatches ``process_row`` / ``process_batch`` calls.

All functions accept and return only picklable types (dicts, lists,
strings, numbers) to cross the Wetlands serialization boundary.

Functions that declare a ``task`` keyword parameter receive a
``RemoteTaskHandle`` injected by Wetlands' module executor, which is
forwarded to the tool if its ``process_row`` / ``process_batch``
method also declares ``task``.
"""

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Optional

from bioimageflow_core.arguments import Arguments, ExecutionContext
from bioimageflow_core.tool import BaseTool, ProcessingTool, IOModel


# Per-file registries: file_path -> {class_name -> class}
_tool_registries: dict[str, dict[str, type]] = {}
# Per-file instances: file_path -> {class_name -> instance}
_instances: dict[str, dict[str, ProcessingTool]] = {}
# Cache: tool_class -> bool (whether process_row accepts 'task')
_accepts_task: dict[type, bool] = {}
# Cache: tool_class -> bool (whether process_batch accepts 'task')
_batch_accepts_task_cache: dict[type, bool] = {}
# Cache: tool_class -> bool (whether process_row accepts 'context')
_accepts_context: dict[type, bool] = {}
# Cache: tool_class -> bool (whether process_batch accepts 'context')
_batch_accepts_context_cache: dict[type, bool] = {}


def _load_versioned_module(config: dict[str, Any]) -> object:
    module_name = str(config["module"])
    package_name = str(config["package"])
    sys_path = str(config["sys_path"])
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    scoped_package = module_name.split(".", 1)[0]
    package_dir = Path(sys_path) / package_name
    init_path = package_dir / "__init__.py"
    if scoped_package not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            scoped_package,
            init_path,
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load package from '{init_path}'")
        package_module = importlib.util.module_from_spec(spec)
        package_module.__package__ = scoped_package
        sys.modules[scoped_package] = package_module
        spec.loader.exec_module(package_module)
    return importlib.import_module(module_name)


def _discover_tools(module: object) -> dict[str, type]:
    """Build a name->class registry from all BaseTool subclasses in the module."""
    registry: dict[str, type] = {}
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseTool) and obj is not BaseTool:
            registry[obj.__name__] = obj
    return registry


def _load_module_from_file(file_path: str) -> object:
    """Load a Python module from a file path."""
    if file_path.startswith("{"):
        config = json.loads(file_path)
        if config.get("mode") == "versioned_module":
            return _load_versioned_module(config)
        if config.get("mode") == "module":
            sys_path = config.get("sys_path")
            if sys_path and sys_path not in sys.path:
                sys.path.insert(0, sys_path)
            return importlib.import_module(config["module"])

    path = Path(file_path)
    module_name = path.stem
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from '{file_path}'")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_instance(tool_file_path: str, tool_class_name: str) -> ProcessingTool:
    """Get or create a cached tool instance for the given file and class."""
    if tool_file_path not in _tool_registries:
        mod = _load_module_from_file(tool_file_path)
        _tool_registries[tool_file_path] = _discover_tools(mod)
        _instances[tool_file_path] = {}
    registry = _tool_registries[tool_file_path]
    instances = _instances[tool_file_path]
    if tool_class_name not in instances:
        if tool_class_name not in registry:
            raise ValueError(
                f"Tool class '{tool_class_name}' not found in '{tool_file_path}'. "
                f"Available: {list(registry.keys())}"
            )
        instances[tool_class_name] = registry[tool_class_name]()
    return instances[tool_class_name]


def _outputs_to_dict(outputs: IOModel) -> dict[str, Any]:
    """Convert an Outputs instance to a plain dict with picklable values."""
    if hasattr(outputs, '_get_all_annotations'):
        d: dict[str, Any] = {}
        for k in outputs._get_all_annotations():
            v = getattr(outputs, k)
            if isinstance(v, Path):
                v = str(v)
            d[k] = v
        return d
    return {k: str(v) if isinstance(v, Path) else v for k, v in vars(outputs).items()}


def _tool_accepts_task(tool: ProcessingTool) -> bool:
    """Check (once per class) whether process_row accepts a 'task' kwarg."""
    cls = type(tool)
    if cls not in _accepts_task:
        sig = inspect.signature(tool.process_row)
        _accepts_task[cls] = 'task' in sig.parameters
    return _accepts_task[cls]


def _batch_accepts_task(tool: ProcessingTool) -> bool:
    """Check (once per class) whether process_batch accepts a 'task' kwarg."""
    cls = type(tool)
    if cls not in _batch_accepts_task_cache:
        sig = inspect.signature(tool.process_batch)
        _batch_accepts_task_cache[cls] = 'task' in sig.parameters
    return _batch_accepts_task_cache[cls]


def _tool_accepts_context(tool: ProcessingTool) -> bool:
    """Check (once per class) whether process_row accepts 'context'."""
    cls = type(tool)
    if cls not in _accepts_context:
        sig = inspect.signature(tool.process_row)
        _accepts_context[cls] = 'context' in sig.parameters
    return _accepts_context[cls]


def _batch_accepts_context(tool: ProcessingTool) -> bool:
    """Check (once per class) whether process_batch accepts 'context'."""
    cls = type(tool)
    if cls not in _batch_accepts_context_cache:
        sig = inspect.signature(tool.process_batch)
        _batch_accepts_context_cache[cls] = 'context' in sig.parameters
    return _batch_accepts_context_cache[cls]


def run_process_row(args_tuple, *, task=None):
    """Dispatch a single-row call to a tool's process_row method.

    ``args_tuple``: ``(tool_file_path, tool_class_name, arguments_dict)``
    or ``(tool_file_path, tool_class_name, arguments_dict, context_dict)``
    ``task``: ``RemoteTaskHandle`` injected by Wetlands via module_executor (optional).

    Returns a list of output dicts (one per output row, usually one).
    """
    if len(args_tuple) == 3:
        tool_file_path, tool_class_name, arguments_dict = args_tuple
        context_dict = None
    else:
        tool_file_path, tool_class_name, arguments_dict, context_dict = args_tuple
    tool = _get_instance(tool_file_path, tool_class_name)
    args = Arguments(**arguments_dict)
    kwargs: dict[str, Any] = {}
    if task is not None and _tool_accepts_task(tool):
        kwargs["task"] = task
    if context_dict is not None and _tool_accepts_context(tool):
        kwargs["context"] = ExecutionContext.from_dict(context_dict)
    result = tool.process_row(args, **kwargs)
    outputs = result if isinstance(result, list) else [result]
    return [_outputs_to_dict(out) for out in outputs]


def run_process_batch(
    tool_file_path: str,
    tool_class_name: str,
    arguments_dicts: list[dict],
    context_dict: Optional[dict] = None,
    *,
    task=None,
) -> list[list[dict]]:
    """Dispatch a batch call to a tool's process_batch method.

    Returns a list of lists of output dicts (one inner list per input row).
    """
    tool = _get_instance(tool_file_path, tool_class_name)
    args_list = [Arguments(**d) for d in arguments_dicts]
    kwargs: dict[str, Any] = {}
    if task is not None and _batch_accepts_task(tool):
        kwargs["task"] = task
    if context_dict is not None and _batch_accepts_context(tool):
        kwargs["context"] = ExecutionContext.from_dict(context_dict)
    results = tool.process_batch(args_list, **kwargs)
    # Auto-wrap list[Outputs] -> list[list[Outputs]] for 1-to-1 batch tools
    if results and not isinstance(results[0], list):
        results = [[r] for r in results]
    return [[_outputs_to_dict(out) for out in row_outputs] for row_outputs in results]
