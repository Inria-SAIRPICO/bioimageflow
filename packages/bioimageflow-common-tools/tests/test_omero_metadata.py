"""Import-safe OMERO declarations and source-fixture package discovery."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from bioimageflow_common_tools._omero_environments import omero_environment
from tests.testkit.tool_loader import record_distribution

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


pytestmark = pytest.mark.package_tools
ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("system,machine", [
    ("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "x86_64"),
])
def test_declaration_modules_do_not_import_worker_dependencies(system, machine):
    code = """
import builtins
import importlib
import platform
import sys
import bioimageflow_core
platform.system = lambda: sys.argv[1]
platform.machine = lambda: sys.argv[2]
original = builtins.__import__
forbidden = {'omero', 'Ice', 'keyring', 'numpy', 'PIL', 'tifffile', 'bioimageflow', 'bioimageflow_server'}
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in forbidden:
        raise AssertionError('OMERO declaration imported worker dependency ' + name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
import bioimageflow_common_tools as common
module = importlib.import_module('bioimageflow_common_tools.omero')
assert common.OmeroDownloadDataset is module.OmeroDownloadDataset
assert common.OmeroUploadImage is module.OmeroUploadImage
assert {'OmeroDownloadDataset', 'OmeroUploadImage'} <= set(common.__all__)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, system, machine], cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("system,machine,supported", [
    ("Darwin", "arm64", True),
    ("Darwin", "x86_64", True),
    ("Linux", "x86_64", True),
    ("Windows", "AMD64", False),
    ("Linux", "aarch64", False),
])
def test_versioned_discovery_preserves_common_tools_without_omero_sdk(
    tmp_path, system, machine, supported,
):
    package_dir = Path(__file__).parents[1]
    version = tomllib.loads((package_dir / "pyproject.toml").read_text())["project"]["version"]
    store = tmp_path / "store"
    target = store / "bioimageflow_common_tools" / version
    shutil.copytree(
        package_dir / "bioimageflow_common_tools", target / "bioimageflow_common_tools",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # A recorded source fixture exercises the public loader; normal built-wheel
    # registration is a separate artifact/publication gate.
    record_distribution(target, "bioimageflow_common_tools", version)
    code = """
import builtins
import platform
import sys
import pandas
from bioimageflow import ToolRegistry
from bioimageflow.tool_loader import load_versioned_package
platform.system = lambda: sys.argv[2]
platform.machine = lambda: sys.argv[3]
original = builtins.__import__
forbidden = {'omero', 'Ice', 'keyring', 'PIL', 'tifffile', 'imageio', 'skimage', 'bioimageflow_server'}
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in forbidden:
        raise AssertionError('Package discovery imported worker dependency ' + name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
package, version = 'bioimageflow_common_tools', sys.argv[4]
registry = ToolRegistry(store_path=sys.argv[1])
metadata = registry.register_package(package, version)
base = {
    'Collect', 'Concat', 'ConnectedComponents', 'CrossJoin', 'Files',
    'FilterTableRows', 'Generate', 'InnerJoin', 'JoinOnColumn', 'LabelOverlaps',
    'Mosaic', 'SelectColumns', 'TableFromCsv', 'WriteTable',
}
omero = {'OmeroDownloadDataset', 'OmeroUploadImage'}
supported = sys.argv[5] == 'yes'
expected = base | omero if supported else base
assert {item.class_name for item in metadata} == expected
module = load_versioned_package(package, version, sys.argv[1])
assert set(module.__all__) == expected
assert all(item.package == package and item.version == version for item in metadata)
if supported:
    selected = [item for item in metadata if item.class_name in omero]
    assert len(selected) == 2
    for item in selected:
        assert item.module == package + '.omero'
        assert {'host', 'port', 'username', 'dataset_id'} <= item.inputs_schema.keys()
        assert 'password' not in item.inputs_schema
        assert item.outputs_schema
        tool = registry.get_class(item.class_name, package=package, version=version)
        assert tool.environment.dependencies['python'] == '3.12'
else:
    # Optional declarations remain explicitly refused, without poisoning the
    # already registered ordinary common package.
    try:
        module.OmeroDownloadDataset
    except ValueError as error:
        assert 'macOS arm64/x86_64 or glibc Linux x86_64' in str(error)
    else:
        raise AssertionError('Unsupported OMERO declaration was admitted')
    assert {item.class_name for item in registry.list_tools()} == base
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(store), system, machine, version, "yes" if supported else "no"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Linux", "aarch64")])
def test_unsupported_omero_import_preserves_import_safe_helpers(system, machine):
    code = """
import importlib
import platform
import sys
platform.system = lambda: sys.argv[1]
platform.machine = lambda: sys.argv[2]
import bioimageflow_common_tools as common
from bioimageflow_common_tools._omero_connection import KEYRING_SERVICE
from bioimageflow_common_tools._omero_pixels import AXES
assert 'OmeroDownloadDataset' not in common.__all__
assert 'OmeroUploadImage' not in common.__all__
assert KEYRING_SERVICE == 'bioimageflow-omero' and AXES == 'TCZYX'
try:
    importlib.import_module('bioimageflow_common_tools.omero')
except ValueError as error:
    assert 'macOS arm64/x86_64 or glibc Linux x86_64' in str(error)
else:
    raise AssertionError('Unsupported OMERO module was imported')
assert 'bioimageflow_common_tools.omero' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code, system, machine], cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("system,machine,tag", [
    ("Darwin", "arm64", "macosx_11_0_universal2"),
    ("Darwin", "x86_64", "macosx_11_0_universal2"),
    ("Linux", "x86_64", "manylinux_2_28_x86_64"),
])
def test_public_recipe_selects_one_official_python312_wheel(system, machine, tag):
    declaration = omero_environment(system, machine)
    assert declaration.dependencies["python"] == "3.12"
    requirements = declaration.dependencies["pip"]
    assert "omero-py==5.23.0" in requirements
    [ice] = [item for item in requirements if item.startswith("zeroc-ice @ ")]
    assert ice.startswith("zeroc-ice @ https://github.com/glencoesoftware/")
    assert f"cp312-cp312-{tag}.whl" in ice
    assert all(";" not in requirement for requirement in requirements)


def test_unsupported_worker_platform_is_explicitly_refused():
    with pytest.raises(ValueError, match="macOS arm64/x86_64 or glibc Linux x86_64"):
        omero_environment("Windows", "AMD64")
