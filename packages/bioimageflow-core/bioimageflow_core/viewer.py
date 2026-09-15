"""Worker-safe portable output-viewing requirements.

These values describe software needed to view an output.  They are deliberately
independent from processing environments and contain no local installation or
discovery state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Mapping, Optional, Sequence, get_args, get_origin

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name


def _nonempty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    return normalized


def _version_specifier(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    specifier = _nonempty_string(value, field_name=field_name)
    try:
        parsed = SpecifierSet(specifier)
    except InvalidSpecifier as exc:
        raise ValueError(f"{field_name} must be a valid PEP 440 specifier.") from exc
    return str(parsed)


@dataclass(frozen=True)
class PackageRequirement:
    """One portable Python distribution requirement for viewing.

    ``distribution`` preserves the author's spelling for presentation while
    ``normalized_name`` exposes the PEP 503 comparison identity.
    """

    distribution: str
    version: Optional[str] = None
    normalized_name: str = field(init=False)

    def __post_init__(self) -> None:
        distribution = _nonempty_string(
            self.distribution,
            field_name="PackageRequirement.distribution",
        )
        try:
            normalized_name = canonicalize_name(distribution, validate=True)
        except InvalidName as exc:
            raise ValueError(
                "PackageRequirement.distribution must be a valid Python "
                "distribution name."
            ) from exc
        version = _version_specifier(
            self.version,
            field_name="PackageRequirement.version",
        )
        object.__setattr__(self, "distribution", distribution)
        object.__setattr__(self, "normalized_name", str(normalized_name))
        object.__setattr__(self, "version", version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "distribution": self.distribution,
            "normalized_name": self.normalized_name,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PackageRequirement":
        if not isinstance(value, Mapping):
            raise TypeError("Package requirement must be an object.")
        fields = {"distribution", "normalized_name", "version"}
        if set(value) != fields:
            raise ValueError(
                "Package requirement fields must be exactly "
                f"{sorted(fields)}; got {sorted(value)}."
            )
        requirement = cls(
            distribution=value["distribution"],
            version=value["version"],
        )
        if value["normalized_name"] != requirement.normalized_name:
            raise ValueError(
                "Package requirement normalized_name does not match distribution."
            )
        return requirement


def _coerce_package_requirement(value: Any) -> PackageRequirement:
    if isinstance(value, PackageRequirement):
        return value
    if isinstance(value, str):
        return PackageRequirement(value)
    if isinstance(value, Mapping):
        return PackageRequirement.from_dict(value)
    raise TypeError(
        "Package requirements must be PackageRequirement, distribution strings, "
        "or their strict wire objects."
    )


def _package_tuple(value: Any, *, field_name: str) -> tuple[PackageRequirement, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of package requirements.")
    result: list[PackageRequirement] = []
    seen: set[str] = set()
    for item in value:
        requirement = _coerce_package_requirement(item)
        if requirement.normalized_name in seen:
            raise ValueError(
                f"{field_name} contains duplicate distribution "
                f"{requirement.normalized_name!r}."
            )
        seen.add(requirement.normalized_name)
        result.append(requirement)
    return tuple(result)


@dataclass(frozen=True)
class NapariRequirement:
    """Portable requirements for opening one output in napari."""

    required_packages: Sequence[PackageRequirement] = field(default_factory=tuple)
    recommended_packages: Sequence[PackageRequirement] = field(default_factory=tuple)
    napari_version: Optional[str] = None
    reader_id: Optional[str] = None

    def __post_init__(self) -> None:
        required = _package_tuple(
            self.required_packages,
            field_name="NapariRequirement.required_packages",
        )
        recommended = _package_tuple(
            self.recommended_packages,
            field_name="NapariRequirement.recommended_packages",
        )
        overlap = {item.normalized_name for item in required} & {
            item.normalized_name for item in recommended
        }
        if overlap:
            raise ValueError(
                "A napari package cannot be both required and recommended: "
                f"{sorted(overlap)}."
            )
        napari_version = _version_specifier(
            self.napari_version,
            field_name="NapariRequirement.napari_version",
        )
        reader_id = (
            None
            if self.reader_id is None
            else _nonempty_string(
                self.reader_id,
                field_name="NapariRequirement.reader_id",
            )
        )
        object.__setattr__(self, "required_packages", required)
        object.__setattr__(self, "recommended_packages", recommended)
        object.__setattr__(self, "napari_version", napari_version)
        object.__setattr__(self, "reader_id", reader_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_packages": [item.to_dict() for item in self.required_packages],
            "recommended_packages": [
                item.to_dict() for item in self.recommended_packages
            ],
            "napari_version": self.napari_version,
            "reader_id": self.reader_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NapariRequirement":
        if not isinstance(value, Mapping):
            raise TypeError("Napari requirement must be an object.")
        fields = {
            "required_packages",
            "recommended_packages",
            "napari_version",
            "reader_id",
        }
        if set(value) != fields:
            raise ValueError(
                "Napari requirement fields must be exactly "
                f"{sorted(fields)}; got {sorted(value)}."
            )
        return cls(
            required_packages=value["required_packages"],
            recommended_packages=value["recommended_packages"],
            napari_version=value["napari_version"],
            reader_id=value["reader_id"],
        )


@dataclass(frozen=True)
class ViewerSpec:
    """Portable viewer metadata attached to one output annotation."""

    napari: Optional[NapariRequirement] = None

    def __post_init__(self) -> None:
        if self.napari is not None and not isinstance(
            self.napari,
            NapariRequirement,
        ):
            raise TypeError("ViewerSpec.napari must be NapariRequirement or None.")

    def to_dict(self) -> dict[str, Any]:
        return {"napari": None if self.napari is None else self.napari.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ViewerSpec":
        if not isinstance(value, Mapping):
            raise TypeError("Viewer specification must be an object.")
        if set(value) != {"napari"}:
            raise ValueError(
                "Viewer specification fields must be exactly ['napari']; "
                f"got {sorted(value)}."
            )
        napari = value["napari"]
        return cls(
            napari=None if napari is None else NapariRequirement.from_dict(napari)
        )


def extract_viewer_spec(annotation: Any) -> Optional[ViewerSpec]:
    """Return the :class:`ViewerSpec` carried by ``Annotated``, if any."""
    if get_origin(annotation) is Annotated:
        specs = [item for item in get_args(annotation)[1:] if isinstance(item, ViewerSpec)]
        if len(specs) > 1:
            raise ValueError("An output annotation may carry at most one ViewerSpec.")
        return specs[0] if specs else None
    return None


def _combine_versions(first: Optional[str], second: Optional[str]) -> Optional[str]:
    if first is None:
        return second
    if second is None:
        return first
    return str(SpecifierSet(f"{first},{second}"))


def _merge_packages(
    values: Sequence[PackageRequirement],
) -> tuple[PackageRequirement, ...]:
    ordered: dict[str, PackageRequirement] = {}
    for item in values:
        previous = ordered.get(item.normalized_name)
        if previous is None:
            ordered[item.normalized_name] = item
        else:
            ordered[item.normalized_name] = PackageRequirement(
                previous.distribution,
                _combine_versions(previous.version, item.version),
            )
    return tuple(ordered.values())


def merge_viewer_specs(*specs: Optional[ViewerSpec]) -> Optional[ViewerSpec]:
    """Additively combine viewer declarations.

    Required packages win over recommendations with the same normalized
    distribution name.  Version clauses are intersected by conjunction.
    """
    napari_specs = [spec.napari for spec in specs if spec and spec.napari]
    if not napari_specs:
        return None
    required_names = {
        item.normalized_name
        for spec in napari_specs
        for item in spec.required_packages
    }
    required = _merge_packages(
        tuple(item for spec in napari_specs for item in spec.required_packages)
        + tuple(
            item
            for spec in napari_specs
            for item in spec.recommended_packages
            if item.normalized_name in required_names
        )
    )
    recommended = _merge_packages(
        tuple(
            item
            for spec in napari_specs
            for item in spec.recommended_packages
            if item.normalized_name not in required_names
        )
    )
    version: Optional[str] = None
    reader_id: Optional[str] = None
    for spec in napari_specs:
        version = _combine_versions(version, spec.napari_version)
        if spec.reader_id is not None:
            if reader_id is not None and reader_id != spec.reader_id:
                raise ValueError(
                    "Cannot combine different napari reader IDs: "
                    f"{reader_id!r} and {spec.reader_id!r}."
                )
            reader_id = spec.reader_id
    return ViewerSpec(
        napari=NapariRequirement(
            required_packages=required,
            recommended_packages=recommended,
            napari_version=version,
            reader_id=reader_id,
        )
    )


def coerce_viewer_spec(value: Any) -> ViewerSpec:
    """Normalize one public viewer-spec argument."""
    if isinstance(value, ViewerSpec):
        return value
    if isinstance(value, NapariRequirement):
        return ViewerSpec(napari=value)
    if isinstance(value, Mapping):
        return ViewerSpec.from_dict(value)
    raise TypeError(
        "Viewer metadata must be ViewerSpec, NapariRequirement, or a strict wire object."
    )
