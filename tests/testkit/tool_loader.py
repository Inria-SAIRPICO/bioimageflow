"""Shared helpers for the focused tests split from ``tests/unit/test_tool_loader.py``."""

# ruff: noqa: F401

import inspect

import json

import sys

import pytest

from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec

from bioimageflow.dataframe_tool import DataFrameTool


@pytest.fixture
def tool_store(tmp_path):
    """Create a tool store with two versions of dummy_tools.

    v1.0.0:
      - AlphaTool (ProcessingTool): Inputs(value: int = 0), Outputs(result: str)
        process_row returns result="v1"
      - LoaderTool (DataFrameTool): Inputs(path: str), Outputs(filepath: str)
      - AlphaTool uses relative import from .base (DummyBase)
      - utils/ subpackage with helpers.py (tests nested relative imports)

    v2.0.0:
      - AlphaTool (ProcessingTool): Inputs(value: int = 0, extra: int = 0),
        Outputs(result: str) — process_row returns result="v2"
      - LoaderTool (DataFrameTool): same schema but adds a "version" column = "v2"
      - Different DummyBase internals (distinct class object)
    """
    store = tmp_path / "tool_packages"

    for version, extra_field, result_value, loader_extra in [
        ("1.0.0", "", "v1", ""),
        ("2.0.0", "\n        extra: int = 0", "v2", "\n        df['version'] = 'v2'"),
    ]:
        pkg_dir = store / "dummy_tools" / version / "dummy_tools"
        pkg_dir.mkdir(parents=True)
        metadata_dir = (
            store
            / "dummy_tools"
            / version
            / f"dummy_tools-{version}.dist-info"
        )
        metadata_dir.mkdir()
        (metadata_dir / "METADATA").write_text(
            "Metadata-Version: 2.1\n"
            "Name: dummy-tools\n"
            f"Version: {version}\n",
            encoding="utf-8",
        )
        (metadata_dir / "top_level.txt").write_text(
            "dummy_tools\n", encoding="utf-8"
        )
        dep_dir = store / "dummy_tools" / version / "dep_pkg"
        dep_dir.mkdir()
        (dep_dir / "__init__.py").write_text("DEP_VALUE = 42\n")

        (pkg_dir / "__init__.py").write_text(
            "from .alpha import AlphaTool\nfrom .loader import LoaderTool\n"
        )

        (pkg_dir / "base.py").write_text(
            "from bioimageflow_core import ProcessingTool, EnvironmentSpec\n"
            "dummy_env = EnvironmentSpec(\n"
            "    name='dummy', dependencies={'pip': ['numpy==2.4.2']}\n"
            ")\n"
            "class DummyBase(ProcessingTool):\n"
            "    environment = dummy_env\n"
        )

        (pkg_dir / "alpha.py").write_text(
            "import dep_pkg\n"
            "from .base import DummyBase\n"
            "from .utils.helpers import helper_func\n"
            "from bioimageflow_core import IOModel, Arguments, RowConsumption\n\n"
            "class AlphaTool(DummyBase):\n"
            "    row_consumption = RowConsumption.MAPPED\n"
            f"    display_name = 'Alpha'\n"
            "    class Inputs(IOModel):\n"
            f"        value: int = 0{extra_field}\n"
            "    class Outputs(IOModel):\n"
            "        result: str\n"
            "    def process_row(self, arguments: Arguments, *, context: object | None = None):\n"
            f"        return self.Outputs(result='{result_value}')\n"
        )

        (pkg_dir / "loader.py").write_text(
            "from pathlib import Path\n"
            "import pandas as pd\n"
            "from bioimageflow.dataframe_tool import DataFrameTool\n"
            "from bioimageflow_core import IOModel\n\n"
            "class LoaderTool(DataFrameTool):\n"
            "    display_name = 'Dummy Loader'\n"
            "    class Inputs(IOModel):\n"
            "        path: str\n"
            "    class Outputs(IOModel):\n"
            "        filepath: str\n"
            "    def transform(self, df, arguments):\n"
            "        p = Path(arguments.path)\n"
            "        files = sorted(p.glob('*.tif'))\n"
            "        df = pd.DataFrame({'filepath': [str(f) for f in files]})\n"
            f"{loader_extra}\n"
            "        return df\n"
        )

        # utils/ subpackage (tests nested relative imports)
        utils_dir = pkg_dir / "utils"
        utils_dir.mkdir()
        (utils_dir / "__init__.py").write_text("from .helpers import helper_func\n")
        (utils_dir / "helpers.py").write_text("def helper_func():\n    return 42\n")

    return store


@pytest.fixture
def lazy_tool_store(tmp_path):
    """Create a tool store with package-level lazy ``__all__`` exports."""
    store = tmp_path / "tool_packages"

    for version, result_value in [
        ("1.0.0", "lazy-v1"),
        ("2.0.0", "lazy-v2"),
    ]:
        pkg_dir = store / "lazy_tools" / version / "lazy_tools"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(
            "from importlib import import_module\n"
            "from typing import Any\n\n"
            "_EXPORTS = {\n"
            "    'LazyAlpha': ('alpha', 'LazyAlpha'),\n"
            "    'LazyLoader': ('loader', 'LazyLoader'),\n"
            "}\n"
            "__all__ = ['LazyAlpha', 'LazyLoader']\n\n"
            "def __getattr__(name: str) -> Any:\n"
            "    try:\n"
            "        module_name, attribute_name = _EXPORTS[name]\n"
            "    except KeyError as exc:\n"
            "        raise AttributeError(name) from exc\n"
            "    module = import_module(f'.{module_name}', __name__)\n"
            "    value = getattr(module, attribute_name)\n"
            "    globals()[name] = value\n"
            "    return value\n"
        )
        (pkg_dir / "alpha.py").write_text(
            "from bioimageflow_core import ProcessingTool, RowConsumption, IOModel, Arguments\n\n"
            "class LazyAlpha(ProcessingTool):\n"
            "    row_consumption = RowConsumption.MAPPED\n"
            "    display_name = 'Lazy Alpha'\n"
            "    class Inputs(IOModel):\n"
            "        value: int = 0\n"
            "    class Outputs(IOModel):\n"
            "        result: str\n"
            "    def process_row(self, arguments: Arguments):\n"
            f"        return self.Outputs(result='{result_value}')\n"
        )
        (pkg_dir / "loader.py").write_text(
            "import pandas as pd\n"
            "from bioimageflow.dataframe_tool import DataFrameTool\n"
            "from bioimageflow_core import IOModel\n\n"
            "class LazyLoader(DataFrameTool):\n"
            "    display_name = 'Lazy Loader'\n"
            "    class Inputs(IOModel):\n"
            "        count: int = 1\n"
            "    class Outputs(IOModel):\n"
            "        value: int\n"
            "    def transform(self, df, arguments):\n"
            "        return pd.DataFrame({'value': list(range(arguments.count))})\n"
        )

    return store


@pytest.fixture
def broken_lazy_tool_store(tmp_path):
    """Create a lazy package whose public export fails to materialize."""
    store = tmp_path / "tool_packages"
    pkg_dir = store / "broken_lazy_tools" / "1.0.0" / "broken_lazy_tools"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "__all__ = ['BrokenTool']\n\n"
        "def __getattr__(name: str):\n"
        "    raise RuntimeError(f'cannot load {name}')\n"
    )
    return store


@pytest.fixture(autouse=True)
def _cleanup_sys_modules():
    """Remove any dummy_tools scoped and canonical modules after each test."""
    yield
    to_remove = [
        k
        for k in sys.modules
        if k.startswith(
            (
                "dummy_tools__",
                "dummy_tools",
                "lazy_tools__",
                "lazy_tools",
                "broken_lazy_tools__",
                "broken_lazy_tools",
            )
        )
    ]
    for k in to_remove:
        del sys.modules[k]
    # Clean sys.path entries that point into tmp tool stores
    sys.path[:] = [
        p
        for p in sys.path
        if not any(
            name in p
            for name in (
                "dummy_tools",
                "lazy_tools",
                "broken_lazy_tools",
            )
        )
    ]
