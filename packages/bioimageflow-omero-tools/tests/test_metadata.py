"""Core-only discovery and ordinary public environment declaration."""

import subprocess
import sys

import pytest

from bioimageflow_omero_tools.environments import omero_environment


pytestmark = pytest.mark.package_tools


def test_declaration_modules_do_not_import_worker_dependencies():
    code = """
import builtins
import bioimageflow_core
original = builtins.__import__
forbidden = {'omero', 'keyring', 'numpy', 'PIL', 'tifffile', 'bioimageflow_server'}
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in forbidden:
        raise AssertionError('Host discovery imported worker dependency ' + name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
import bioimageflow_omero_tools
assert bioimageflow_omero_tools.__all__ == ['OmeroDownloadDataset', 'OmeroUploadImage']
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


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
