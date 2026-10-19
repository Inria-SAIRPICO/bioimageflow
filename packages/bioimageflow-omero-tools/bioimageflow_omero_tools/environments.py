"""Public, explicitly pinned OMERO recipes for supported local workers."""

import platform

from bioimageflow_core import EnvironmentSpec


_WHEELS = {
    ("Darwin", "arm64"): "https://github.com/glencoesoftware/zeroc-ice-py-macos-universal2/releases/download/20240131/zeroc_ice-3.6.5-cp312-cp312-macosx_11_0_universal2.whl",
    ("Darwin", "x86_64"): "https://github.com/glencoesoftware/zeroc-ice-py-macos-universal2/releases/download/20240131/zeroc_ice-3.6.5-cp312-cp312-macosx_11_0_universal2.whl",
    ("Linux", "x86_64"): "https://github.com/glencoesoftware/zeroc-ice-py-linux-x86_64/releases/download/20240202/zeroc_ice-3.6.5-cp312-cp312-manylinux_2_28_x86_64.whl",
}


def omero_environment(system: str, machine: str) -> EnvironmentSpec:
    """Select one official Ice wheel without unsupported environment markers."""
    try:
        wheel = _WHEELS[(system, machine)]
    except KeyError:
        raise ValueError(
            "OMERO tools require macOS arm64/x86_64 or glibc Linux x86_64."
        ) from None
    return EnvironmentSpec(
        name="bioimageflow-omero",
        dependencies={
            "python": "3.12",
            "pip": [
                "omero-py==5.23.0",
                f"zeroc-ice @ {wheel}",
                "numpy==2.5.0",
                "Pillow==12.3.0",
                "tifffile==2026.6.1",
                "keyring==25.7.0",
            ],
        },
    )


OMERO_ENV = omero_environment(platform.system(), platform.machine())
