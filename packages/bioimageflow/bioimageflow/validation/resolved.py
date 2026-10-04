"""Detached resolved output authority shared by graph and portable consumers."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Mapping, Optional, cast

from bioimageflow_core import (
    Connectable,
    GUIMeta,
    ImageSpec,
    Layout,
    PathPicker,
    Semantic,
    SharedArray,
)
from bioimageflow_core.viewer import ViewerSpec
from .models import SchemaSerializationError
from .type_descriptors import decode_annotation, encode_annotation


@dataclass(frozen=True)
class ResolvedPort:
    annotation: Any
    _wire: Mapping[str, Any]

    @property
    def image_spec(self) -> ImageSpec | None:
        value = self._wire.get("image_spec")
        if value is None:
            return None
        return ImageSpec(
            semantics={Semantic(item) for item in value["semantics"]},
            layouts={Layout(item) for item in value["layouts"]},
            dtypes=set(value["dtypes"]),
            formats=set(value["formats"]),
        )

    def to_wire(self) -> dict[str, Any]:
        return deepcopy(dict(self._wire))


@dataclass(frozen=True)
class ResolvedSchema:
    state: str
    _ports: tuple[tuple[str, ResolvedPort], ...] = ()

    @property
    def ports(self) -> Mapping[str, ResolvedPort]:
        return MappingProxyType(dict(self._ports))

    def get(self, name: str) -> ResolvedPort | None:
        return self.ports.get(name)

    def to_wire(self) -> dict[str, Any] | None:
        if self.state == "dynamic":
            return None
        return {name: port.to_wire() for name, port in self._ports}

    @classmethod
    def from_columns(
        cls,
        columns: Mapping[str, Any] | None,
        *,
        annotations: Mapping[str, Any] | None = None,
    ) -> ResolvedSchema:
        if columns is None:
            return cls("dynamic")
        if not isinstance(columns, Mapping):
            raise SchemaSerializationError(
                "Resolved columns must be a mapping or unknown"
            )
        annotations = annotations or {}
        ports = []
        for name, entry in columns.items():
            if name == "_passthrough":
                raise SchemaSerializationError(
                    "Passthrough must be resolved from upstream before semantic admission"
                )
            if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
                raise SchemaSerializationError(
                    "Resolved columns require named field records"
                )
            wire = deepcopy(dict(entry))
            annotation = annotations.get(name)
            if annotation is None:
                spec = wire.get("type_spec")
                if spec is None:
                    # Current dynamic authoring callbacks expose finite labels;
                    # capture their semantic descriptor immediately, never eval.
                    labels = {
                        "Any": Any,
                        "any": Any,
                        "int": int,
                        "str": str,
                        "float": float,
                        "bool": bool,
                        "list": list,
                        "tuple": tuple,
                        "dict": dict,
                        "Path": Path,
                        "ImageFile": Path,
                        "ImageShared": SharedArray,
                    }
                    label = wire.get("type")
                    if label not in labels:
                        raise SchemaSerializationError(
                            f"Unsupported resolved type label: {label!r}"
                        )
                    annotation = labels[label]
                    if wire.get("nullable"):
                        annotation = cast(Any, Optional)[annotation]
                    wire["type_spec"] = encode_annotation(annotation)
                else:
                    annotation = decode_annotation(spec)
                metadata = []
                image = wire.get("image_spec")
                if image is not None:
                    metadata.append(
                        ImageSpec(
                            semantics={Semantic(item) for item in image["semantics"]},
                            layouts={Layout(item) for item in image["layouts"]},
                            dtypes=set(image["dtypes"]),
                            formats=set(image["formats"]),
                        )
                    )
                gui_fields = {
                    key: wire[key]
                    for key in (
                        "display_name",
                        "description",
                        "min",
                        "max",
                        "step",
                        "group",
                    )
                    if wire.get(key) is not None
                }
                if wire.get("connectable") is not None:
                    gui_fields["connectable"] = Connectable(wire["connectable"])
                if wire.get("path_picker") is not None:
                    gui_fields["path_picker"] = PathPicker(wire["path_picker"])
                if gui_fields:
                    metadata.append(GUIMeta(**gui_fields))
                if wire.get("viewer") is not None:
                    metadata.append(ViewerSpec.from_dict(wire["viewer"]))
                if metadata:
                    annotation = cast(Any, Annotated)[tuple([annotation, *metadata])]
            else:
                wire["type_spec"] = encode_annotation(annotation)
            ports.append((name, ResolvedPort(annotation, MappingProxyType(wire))))
        return cls("known", tuple(ports))
