"""A current graph editing authority with detached materializations.

Every edit admits a proposed graph before replacing the session revision.
Previously returned Workflows remain independent definitions; the materialized
cache is only a projection of one accepted session revision.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from bioimageflow.engine import NodePlan
from bioimageflow.validation import ValidationError, serialize_constant
from bioimageflow.workflow import Workflow, _absolute_runtime_path


class WorkflowSession:
    """A mutable, dict-backed editing session for a workflow.

    The session is the canonical state. Call :meth:`to_workflow` to
    obtain a built :class:`Workflow` for execution, or :meth:`to_dict`
    to snapshot the wire format.
    """

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        storage_path: str | Path,
        registry: Any | None = None,
    ) -> None:
        """Create an editing session with runtime storage kept outside its graph."""
        if data is None:
            data = {
                "schema_version": 2,
                "name": "workflow",
                "display_name": "workflow",
                "interface": {"inputs": [], "outputs": []},
                "nodes": [],
                "edges": [],
                "config": {},
            }
        else:
            data = deepcopy(data)

        self._data: dict[str, Any] = data
        self._registry = registry

        # Materialization belongs to exactly this accepted graph revision.
        self._workflow_cache: Workflow | None = None
        self._validate_cache: list[ValidationError] | None = None
        self.storage_path = storage_path

    @property
    def storage_path(self) -> Path:
        """Return the normalized runtime storage root."""
        return self._storage_path

    @storage_path.setter
    def storage_path(self, value: str | Path) -> None:
        """Assign one runtime storage root to this session and its materialization."""
        normalized = _absolute_runtime_path(value)
        self._storage_path = normalized
        self._workflow_cache = None
        self._validate_cache = None

    # ------------------------------------------------------------------
    # Dict shape helpers
    # ------------------------------------------------------------------

    @property
    def _nodes_list(self) -> list[dict[str, Any]]:
        return self._data.setdefault("nodes", [])

    @property
    def _edges_list(self) -> list[dict[str, Any]]:
        return self._data.setdefault("edges", [])

    def _get_node_dict(self, name: str) -> dict[str, Any]:
        for nd in self._nodes_list:
            if nd["name"] == name:
                return nd
        raise KeyError(f"Node '{name}' not in session.")

    def _commit(self, candidate: dict[str, Any]) -> None:
        """Admit a detached candidate before publishing a new graph revision."""
        workflow, errors = Workflow.from_dict(
            candidate, storage_path=self.storage_path, validate_only=True,
            partial=True, auto_install=False,
        )
        refused = next((error for error in errors if error.kind in {
            "unknown_input", "duplicate_name", "column_not_found", "type_mismatch",
        }), None)
        if refused is not None:
            raise ValueError(refused.message)
        self._data = candidate
        self._workflow_cache = workflow
        self._validate_cache = None

    def add_node(self, node: dict[str, Any]) -> None:
        candidate = self.to_dict()
        if any(item["name"] == node["name"] for item in candidate["nodes"]):
            raise ValueError(f"Node '{node['name']}' already exists.")
        candidate["nodes"].append(deepcopy(node))
        self._commit(candidate)

    def remove_node(self, name: str) -> None:
        self._get_node_dict(name)
        candidate = self.to_dict()
        candidate["nodes"] = [node for node in candidate["nodes"] if node["name"] != name]
        candidate["edges"] = [edge for edge in candidate["edges"] if name not in (edge["source_node"], edge["target_node"])]
        interface = candidate["interface"]
        interface["outputs"] = [port for port in interface["outputs"] if port["source"]["node"] != name]
        for port in interface["inputs"]:
            port["targets"] = [target for target in port["targets"] if target["node"] != name]
        interface["inputs"] = [port for port in interface["inputs"] if port["targets"]]
        self._commit(candidate)

    def add_edge(self, edge: dict[str, Any]) -> None:
        candidate = self.to_dict()
        candidate["edges"].append(deepcopy(edge))
        self._commit(candidate)

    def remove_edge(self, edge_id: str) -> None:
        candidate = self.to_dict()
        edges = candidate["edges"]
        candidate["edges"] = [edge for edge in edges if edge["id"] != edge_id]
        if len(edges) == len(candidate["edges"]):
            raise KeyError(f"Edge with id '{edge_id}' not found.")
        self._commit(candidate)

    def set_constant(self, node: str, field: str, value: Any) -> None:
        """Replace a field's edge with an owned constant in one revision."""
        self._get_node_dict(node)
        candidate = self.to_dict()
        entry = next(item for item in candidate["nodes"] if item["name"] == node)
        key = "bindings" if entry["type"] == "workflow" else "constants"
        entry.setdefault(key, {})[field] = serialize_constant(value)
        candidate["edges"] = [edge for edge in candidate["edges"] if not (
            edge["target_node"] == node and edge.get("target_input") == field
        )]
        self._commit(candidate)

    def set_enabled(self, node: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        self._get_node_dict(node)
        candidate = self.to_dict()
        entry = next(item for item in candidate["nodes"] if item["name"] == node)
        if enabled:
            entry.pop("enabled", None)
        else:
            entry["enabled"] = False
        self._commit(candidate)

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return {nd["name"]: deepcopy(nd) for nd in self._nodes_list}

    @property
    def edges(self) -> list[dict[str, Any]]:
        return [deepcopy(e) for e in self._edges_list]

    @property
    def errors(self) -> list[ValidationError]:
        """Cached errors from the last :meth:`validate` call (or empty)."""
        return list(self._validate_cache or [])

    @property
    def failed_nodes(self) -> dict[str, ValidationError]:
        """Failed nodes from the last :meth:`to_workflow` build."""
        if self._workflow_cache is None:
            return {}
        return self._workflow_cache.failed_nodes

    # ------------------------------------------------------------------
    # Materialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the wire-format snapshot. The returned dict is a copy."""
        return deepcopy(self._data)

    def to_workflow(self) -> Workflow:
        """Return a built :class:`Workflow`, caching the result.

        Uses ``Workflow.from_dict(partial=True, validate_only=True)`` so
        per-node failures are captured in
        :attr:`Workflow.failed_nodes` rather than raising.
        """
        if self._workflow_cache is not None:
            return self._workflow_cache
        wf, _errors = Workflow.from_dict(
            self._data,
            storage_path=self.storage_path,
            validate_only=True,
            partial=True,
            auto_install=False,
        )
        self._workflow_cache = wf
        return wf

    def validate(self) -> list[ValidationError]:
        """Return the validation errors for the current state.

        Cached only for the accepted graph revision.
        """
        if self._validate_cache is not None:
            return list(self._validate_cache)
        wf = self.to_workflow()
        errs = list(wf.errors) + list(wf.validate())
        # Deduplicate on the same key Workflow.validate() uses.
        seen: set[tuple[Any, ...]] = set()
        unique: list[ValidationError] = []
        for e in errs:
            key = (e.path, e.node, e.field, e.kind, e.message, e.edge_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        self._validate_cache = unique
        return list(unique)

    def plan(self) -> dict[str, NodePlan]:
        """Return a fresh :meth:`Workflow.plan` for the current state."""
        wf = self.to_workflow()
        return dict(wf.plan())

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        storage_path: str | Path,
        registry: Any | None = None,
    ) -> "WorkflowSession":
        return cls(data, storage_path=storage_path, registry=registry)
