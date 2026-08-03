"""Strict workflow definition payloads for submitted execution."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from bioimageflow.storage import canonical_json_bytes
from bioimageflow.validation.constants import serialize_constant
from bioimageflow.workflow import Workflow

from .errors import LauncherProtocolError


_PAYLOAD_KINDS = frozenset({"graph_v1", "archive_v1"})


def _digest_payload(payload: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _validate_executable_workflow(workflow: Workflow) -> None:
    if workflow.is_partial or workflow.failed_nodes or workflow.errors:
        raise LauncherProtocolError(
            "Partial workflows and workflows with unresolved nodes cannot be submitted.",
            details={
                "failed_nodes": sorted(workflow.failed_nodes),
                "is_partial": workflow.is_partial,
                "errors": [
                    {"kind": error.kind, "message": error.message}
                    for error in workflow.errors
                ],
            },
        )
    errors = workflow.validate()
    if errors:
        raise LauncherProtocolError(
            "Workflow validation failed before submission.",
            details={
                "errors": [
                    {
                        "kind": error.kind,
                        "message": error.message,
                        "node": error.node,
                        "field": error.field,
                        "path": list(error.path),
                    }
                    for error in errors
                ]
            },
        )


def serialize_workflow_payload(workflow: Workflow) -> dict[str, Any]:
    """Capture one strict graph or archive payload without runtime storage."""
    if not isinstance(workflow, Workflow):
        raise TypeError("workflow must be a Workflow.")
    _validate_executable_workflow(workflow)
    payload = workflow.to_dict(include_custom_tools=True)
    kind = "archive_v1" if payload.get("archive_version") == 1 else "graph_v1"
    if kind == "graph_v1" and payload.get("schema_version") != 1:
        raise LauncherProtocolError("Workflow graph payload has an invalid version.")
    if kind == "archive_v1":
        if set(payload) != {"archive_version", "workflow", "custom_sources"}:
            raise LauncherProtocolError(
                "Workflow archive payload contains invalid fields."
            )
        sources = payload.get("custom_sources")
        if not isinstance(sources, list):
            raise LauncherProtocolError(
                "Workflow archive custom_sources must be an array."
            )
        source_ids = [
            source.get("id") for source in sources if isinstance(source, dict)
        ]
        if len(source_ids) != len(sources) or len(set(source_ids)) != len(source_ids):
            raise LauncherProtocolError(
                "Workflow archive custom sources must have unique IDs."
            )
    if "storage_path" in payload.get("config", {}):
        raise LauncherProtocolError(
            "Runtime storage must not enter a submitted workflow graph."
        )
    return {
        "kind": kind,
        "digest": _digest_payload(payload),
        "payload": copy.deepcopy(payload),
    }


def replace_workflow_payload_constants(
    value: dict[str, Any],
    replacements: tuple[tuple[str, str, Any], ...],
) -> dict[str, Any]:
    """Replace scoped constants in a fresh payload and bind a new digest."""
    result = copy.deepcopy(value)
    payload = result["payload"]
    graph = payload["workflow"] if result["kind"] == "archive_v1" else payload
    for scoped_node_path, input_name, replacement in replacements:
        current = graph
        parts = scoped_node_path.split("/")
        for position, part in enumerate(parts):
            nodes = current.get("nodes")
            if not isinstance(nodes, list):
                raise LauncherProtocolError("Submitted workflow graph is malformed.")
            matches = [
                node
                for node in nodes
                if isinstance(node, dict) and node.get("name") == part
            ]
            if len(matches) != 1:
                raise LauncherProtocolError(
                    f"Scoped node path {scoped_node_path!r} is not unique."
                )
            node = matches[0]
            if position < len(parts) - 1:
                nested = node.get("workflow")
                if not isinstance(nested, dict):
                    raise LauncherProtocolError(
                        f"Scoped node path {scoped_node_path!r} is invalid."
                    )
                current = nested
                continue
            constants = node.get("constants")
            if not isinstance(constants, dict):
                raise LauncherProtocolError(
                    f"Node {scoped_node_path!r} has invalid constants."
                )
            constants[input_name] = serialize_constant(replacement)
    result["digest"] = _digest_payload(payload)
    return result


def load_workflow_payload(
    value: Any,
    *,
    storage_path: str | Path,
) -> Workflow:
    """Verify and materialize a submitted workflow with explicit runtime storage."""
    if not isinstance(value, dict) or set(value) != {"kind", "digest", "payload"}:
        raise LauncherProtocolError(
            "Workflow payload requires exactly kind, digest, and payload."
        )
    kind = value["kind"]
    payload = value["payload"]
    if kind not in _PAYLOAD_KINDS or not isinstance(payload, dict):
        raise LauncherProtocolError("Submitted workflow payload kind is invalid.")
    if _digest_payload(payload) != value["digest"]:
        raise LauncherProtocolError("Submitted workflow payload digest mismatch.")
    if kind == "graph_v1" and payload.get("schema_version") != 1:
        raise LauncherProtocolError("Submitted graph payload version mismatch.")
    if kind == "archive_v1" and payload.get("archive_version") != 1:
        raise LauncherProtocolError("Submitted archive payload version mismatch.")
    try:
        workflow = Workflow.from_dict(
            copy.deepcopy(payload),
            storage_path=storage_path,
            auto_install=False,
        )
    except Exception as exc:
        raise LauncherProtocolError(
            "Submitted workflow payload could not be materialized."
        ) from exc
    _validate_executable_workflow(workflow)
    if workflow.storage_path != Path(storage_path).expanduser().absolute():
        raise LauncherProtocolError(
            "Materialized workflow did not retain the assigned runtime storage."
        )
    return workflow
