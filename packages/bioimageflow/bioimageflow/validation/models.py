"""Focused orchestrator validation behavior."""

from __future__ import annotations

from .common import (
    Literal,
    dataclass,
)


class SchemaSerializationError(Exception):
    """Raised when :func:`serialize_input_schema` / :func:`serialize_output_schema`
    cannot produce a wire-format schema for a tool class — typically because
    the tool class could not be instantiated for introspection.
    """


ValidationErrorKind = Literal[
    "cycle",
    "type_mismatch",
    "missing_input",
    "unknown_input",
    "column_not_found",
    "parameter_invalid",
    "unknown_tool",
    "duplicate_name",
    "construction_failed",
    "source_tool_upstream",
]


@dataclass(frozen=True)
class ValidationError:
    """A single problem found during graph construction or validation.

    Instances are produced by :meth:`Workflow.capture_errors`,
    ``Workflow.from_dict(..., partial=True)``, and ``Workflow.validate()``.
    Consumers (GUIs, linters) map these to their own display formats. The
    library never raises ``ValidationError``; it raises the existing
    exceptions unless an error-collector is active.

    ``edge`` carries the structural ``(from_node, to_node, field)`` triple.
    ``edge_id`` is an optional opaque identifier that GUIs can attach to
    edges via the ``id`` key in the wire format; the library round-trips
    it through :meth:`Workflow.to_dict` / :meth:`Workflow.from_dict` and
    copies it onto every ``ValidationError`` raised against that edge.
    This is the disambiguator for cases like positional args, where
    multiple edges share the same triple by construction.
    """

    kind: ValidationErrorKind
    message: str
    node: str | None = None
    field: str | None = None
    edge: tuple[str, str, str] | None = None
    edge_id: str | None = None
    path: tuple[str, ...] = ()
