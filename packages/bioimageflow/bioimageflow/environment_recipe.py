"""Pure translation of augmented declarations into the provider recipe."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any
import urllib.parse
import urllib.request

from wetlands import EnvironmentSpec, LocalPackage

_LOCAL_PYPI_REFERENCE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_.-]+)"
    r"(?:\[(?P<extras>[A-Za-z0-9_.,-]+)\])?"
    r"\s*@\s*(?P<url>file://\S+)\s*$"
)


def _translate_conda(
    values: list[Any],
    channels: list[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    translated: list[str] = []
    prefix_channels: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"Wetlands 2 Conda dependencies must be strings, got {value!r}.")
        if "::" in value:
            channel, value = value.split("::", 1)
            if channel:
                prefix_channels.append(channel)
        translated.append(value)
    ordered_channels = (
        [*prefix_channels, "conda-forge"]
        if channels is None
        else [*channels, *prefix_channels]
    )
    return tuple(translated), tuple(dict.fromkeys(ordered_channels))


def _translate_local_dependency(value: Any) -> LocalPackage:
    if not isinstance(value, dict) or "path" not in value:
        raise TypeError("Wetlands 2 local dependencies require a mapping with 'path'.")
    package = LocalPackage(
        source=Path(str(value["path"])),
        editable=bool(value.get("editable", False)),
        extras=tuple(value.get("extras", ())),
    )
    declared = value.get("name")
    if isinstance(declared, str):
        canonical = declared.replace("_", "-").lower()
        if canonical != package.distribution_name:
            raise ValueError(
                f"Local dependency declares {declared!r}, but its project is "
                f"{package.distribution_name!r}."
            )
    return package


def _translate_pypi_dependencies(
    values: list[Any],
) -> tuple[tuple[str, ...], tuple[LocalPackage, ...]]:
    pypi: list[str] = []
    local: list[LocalPackage] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("Wetlands 2 PyPI dependencies must be strings.")
        match = _LOCAL_PYPI_REFERENCE.fullmatch(value)
        if match is None:
            pypi.append(value)
            continue
        parsed = urllib.parse.urlparse(match.group("url"))
        if parsed.scheme != "file" or parsed.query or parsed.fragment:
            raise ValueError(f"Invalid local file dependency {value!r}.")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Local file dependencies must not use a remote host.")
        source = Path(
            urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
        )
        extras = tuple(
            extra
            for extra in (match.group("extras") or "").split(",")
            if extra
        )
        package = LocalPackage(source=source, extras=extras)
        expected = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        if expected != package.distribution_name:
            raise ValueError(
                f"Local dependency declares {match.group('name')!r}, but its "
                f"project is {package.distribution_name!r}."
            )
        local.append(package)
    return tuple(pypi), tuple(local)


def to_wetlands_spec(dependencies: Mapping[str, Any]) -> EnvironmentSpec:
    allowed = {"python", "conda", "pip", "channels", "local"}
    unknown = sorted(set(dependencies).difference(allowed))
    if unknown:
        raise ValueError(
            "Unsupported BioImageFlow environment dependency section(s) for "
            f"Wetlands 2: {', '.join(unknown)}."
        )
    python = dependencies.get("python", ">=3.9")
    if not isinstance(python, str):
        raise TypeError("Environment 'python' must be a version string.")
    if re.fullmatch(r"[0-9]+\.[0-9]+", python):
        python = f"{python}.*"
    raw_conda = list(dependencies.get("conda", []))
    raw_channels = (
        list(dependencies["channels"]) if "channels" in dependencies else None
    )
    if raw_channels is not None and any(
        not isinstance(channel, str) for channel in raw_channels
    ):
        raise TypeError("Environment channels must be strings.")
    conda, channels = _translate_conda(raw_conda, raw_channels)
    pypi, local_from_pypi = _translate_pypi_dependencies(
        list(dependencies.get("pip", ()))
    )
    explicit_local = tuple(
        _translate_local_dependency(item)
        for item in dependencies.get("local", ())
    )
    local = local_from_pypi + explicit_local
    return EnvironmentSpec(
        python=python,
        conda=conda,
        pypi=pypi,
        channels=channels,
        local=local,
    )

