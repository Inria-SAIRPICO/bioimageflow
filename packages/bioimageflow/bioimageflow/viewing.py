"""Portable, derived viewing-requirement archive snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from bioimageflow_core import ViewerSpec


VIEWING_REQUIREMENTS_SCHEMA = "bioimageflow.viewing_requirements.v1"


@dataclass(frozen=True)
class ViewingRequirementEntry:
    """Viewing metadata for one scoped output identity."""

    status: Literal["known", "unknown"]
    viewer: ViewerSpec | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"known", "unknown"}:
            raise ValueError("Viewing requirement status must be known or unknown.")
        if self.status == "known" and self.reason is not None:
            raise ValueError("Known viewing requirements cannot carry an unknown reason.")
        if self.status == "unknown" and self.viewer is not None:
            raise ValueError("Unknown viewing requirements cannot carry a viewer spec.")
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason):
            raise ValueError("Viewing requirement reason must be a non-empty string.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "viewer": None if self.viewer is None else self.viewer.to_dict(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ViewingRequirementEntry":
        if not isinstance(value, Mapping) or set(value) != {
            "status",
            "viewer",
            "reason",
        }:
            raise ValueError("Malformed viewing requirement entry.")
        viewer = value["viewer"]
        return cls(
            status=value["status"],
            viewer=None if viewer is None else ViewerSpec.from_dict(viewer),
            reason=value["reason"],
        )


@dataclass(frozen=True)
class ViewingRequirementsManifest:
    """Versioned export snapshot readable without loading tool packages."""

    outputs: Mapping[str, ViewingRequirementEntry] = field(default_factory=dict)
    complete: bool = True
    schema: str = VIEWING_REQUIREMENTS_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != VIEWING_REQUIREMENTS_SCHEMA:
            raise ValueError("Unsupported viewing requirements manifest schema.")
        normalized: dict[str, ViewingRequirementEntry] = {}
        for identity, entry in self.outputs.items():
            if not isinstance(identity, str) or not identity:
                raise ValueError("Scoped output identities must be non-empty strings.")
            if not isinstance(entry, ViewingRequirementEntry):
                raise TypeError("Manifest outputs must contain ViewingRequirementEntry values.")
            normalized[identity] = entry
        if self.complete != all(entry.status == "known" for entry in normalized.values()):
            raise ValueError("Manifest completeness does not match its output entries.")
        object.__setattr__(self, "outputs", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "complete": self.complete,
            "outputs": {
                identity: entry.to_dict()
                for identity, entry in sorted(self.outputs.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ViewingRequirementsManifest":
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "complete",
            "outputs",
        }:
            raise ValueError("Malformed viewing requirements manifest.")
        if not isinstance(value["complete"], bool) or not isinstance(
            value["outputs"], Mapping
        ):
            raise ValueError("Malformed viewing requirements manifest values.")
        return cls(
            schema=value["schema"],
            complete=value["complete"],
            outputs={
                identity: ViewingRequirementEntry.from_dict(entry)
                for identity, entry in value["outputs"].items()
            },
        )


def _output_identity(scope: str, kind: str, output: str) -> str:
    return f"{scope}::{kind}/{output}"


def derive_viewing_requirements(workflow: Any) -> ViewingRequirementsManifest:
    """Derive the export snapshot from authoritative live graph metadata."""
    from bioimageflow.workflow_node import WorkflowNode

    outputs: dict[str, ViewingRequirementEntry] = {}

    def visit(definition: Any, scope: str) -> None:
        for name, node in definition._nodes.items():
            node_scope = f"{scope}/{name}" if scope else name
            if isinstance(node, WorkflowNode):
                for output_id in node.workflow._interface_outputs:
                    outputs[_output_identity(
                        node_scope,
                        "workflow-output",
                        output_id,
                    )] = ViewingRequirementEntry(
                        status="known",
                        viewer=node.get_output_viewer_spec(output_id),
                    )
                visit(node.workflow, node_scope)
                continue
            schema = node.get_output_schema()
            if schema is None:
                outputs[_output_identity(node_scope, "tool-output", "*")] = (
                    ViewingRequirementEntry(
                        status="unknown",
                        reason="The authoritative tool output schema is unavailable.",
                    )
                )
                for output, addition in node.viewer_additions.items():
                    outputs[_output_identity(node_scope, "tool-output", output)] = (
                        ViewingRequirementEntry(status="known", viewer=addition)
                    )
                continue
            for output in sorted(set(schema) - {"_passthrough"}):
                outputs[_output_identity(node_scope, "tool-output", output)] = (
                    ViewingRequirementEntry(
                        status="known",
                        viewer=node.get_output_viewer_spec(output),
                    )
                )

    visit(workflow, "")
    for output_id in workflow._interface_outputs:
        outputs[_output_identity("@workflow", "workflow-output", output_id)] = (
            ViewingRequirementEntry(
                status="known",
                viewer=workflow.get_output_viewer_spec(output_id),
            )
        )
    expected = workflow._expected_node_names
    if expected is not None:
        for missing in sorted(expected - set(workflow._nodes)):
            outputs[_output_identity(missing, "tool-output", "*")] = (
                ViewingRequirementEntry(
                    status="unknown",
                    reason="The node's tool metadata could not be loaded.",
                )
            )
    return ViewingRequirementsManifest(
        outputs=outputs,
        complete=all(entry.status == "known" for entry in outputs.values()),
    )


def inspect_viewing_requirements(
    value: Mapping[str, Any] | str | Path,
) -> ViewingRequirementsManifest:
    """Read an archive snapshot without importing or installing tool packages."""
    import json
    import zipfile

    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                document = json.loads(archive.read("workflow.json"))
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
    else:
        document = value
    if not isinstance(document, Mapping):
        raise TypeError("Workflow document must be an object.")
    if document.get("archive_version") == 1:
        if set(document) != {"archive_version", "workflow", "custom_sources"}:
            raise ValueError("Malformed version-1 workflow archive envelope.")
        return ViewingRequirementsManifest()
    if document.get("archive_version") != 2 or set(document) != {
        "archive_version",
        "workflow",
        "custom_sources",
        "viewing_requirements",
    }:
        raise ValueError("Viewing requirements require a version-2 archive envelope.")
    return ViewingRequirementsManifest.from_dict(document["viewing_requirements"])


__all__ = [
    "ViewingRequirementEntry",
    "ViewingRequirementsManifest",
    "derive_viewing_requirements",
    "inspect_viewing_requirements",
]
