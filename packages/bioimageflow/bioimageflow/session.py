"""Incremental editing session for GUI clients.

A :class:`WorkflowSession` is a parallel data model to :class:`Workflow`
that holds the wire-format dict and exposes mutation operations. It
materializes a :class:`Workflow` on demand and caches it across edits,
selectively rebuilding only when structural changes (add/remove
node/edge) demand it. Edits that don't change the graph topology
(``set_constant``, ``set_enabled``) mutate the cached workflow in
place — so a constant edit followed by ``validate()`` does not
re-resolve any tool class.

Why a separate class? :class:`Workflow` builds nodes eagerly during
construction, with ``_upstream_nodes`` references and column bindings
wired at ``__init__`` time. Retrofitting incremental mutation onto that
model would require invasive changes to ``Node``. A dict-backed
session, materialized to a Workflow only when needed, is both simpler
and matches what GUIs actually want to send over the wire.
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
                "schema_version": 1,
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

        # Cached materialization. `_workflow_cache` is invalidated on
        # structural edits; non-structural edits (constants, enabled
        # flag) update the cached workflow in place to avoid tool
        # re-resolution.
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
        if self._workflow_cache is not None:
            self._workflow_cache._inherit_runtime_storage(normalized)
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

    def _invalidate_structural(self) -> None:
        self._workflow_cache = None
        self._validate_cache = None

    def _invalidate_compute_caches(self) -> None:
        # Edits that don't require a workflow rebuild still invalidate
        # the validation cache. ``plan()`` intentionally refreshes the
        # storage-facing cache snapshot on every call.
        self._validate_cache = None

    # ------------------------------------------------------------------
    # Mutating operations
    # ------------------------------------------------------------------

    def add_node(self, node: dict[str, Any]) -> None:
        """Append a node entry to the session.

        ``node`` is a wire-format dict with at least ``name`` and tool
        identification keys for either a tool or recursive workflow node.
        """
        if any(nd["name"] == node["name"] for nd in self._nodes_list):
            raise ValueError(f"Node '{node['name']}' already exists.")
        self._nodes_list.append(deepcopy(node))
        self._invalidate_structural()

    def remove_node(self, name: str) -> None:
        """Remove a node and all edges touching it."""
        self._data["nodes"] = [
            nd for nd in self._nodes_list if nd["name"] != name
        ]
        self._data["edges"] = [
            e for e in self._edges_list
            if e.get("source_node") != name and e.get("target_node") != name
        ]
        self._invalidate_structural()

    def add_edge(self, edge: dict[str, Any]) -> None:
        """Append one strict column or DataFrame edge record."""
        for key in ("type", "id", "source_node", "target_node"):
            if key not in edge:
                raise ValueError(f"Edge missing required key '{key}'.")
        if edge["type"] not in {"column", "dataframe"}:
            raise ValueError("Unknown edge type.")
        self._edges_list.append(deepcopy(edge))
        self._invalidate_structural()

    def remove_edge(self, edge_id: str) -> None:
        """Remove an edge by its opaque ``id`` field."""
        before = len(self._edges_list)
        self._data["edges"] = [
            e for e in self._edges_list if e.get("id") != edge_id
        ]
        if len(self._edges_list) == before:
            raise KeyError(f"Edge with id '{edge_id}' not found.")
        self._invalidate_structural()

    def set_constant(self, node: str, field: str, value: Any) -> None:
        """Update a constant binding on a node.

        This is a non-structural edit: the cached :class:`Workflow` (if
        any) is mutated in place so that subsequent
        :meth:`to_workflow` / :meth:`validate` / :meth:`plan` calls do
        not re-resolve any tool class.
        """
        node_dict = self._get_node_dict(node)
        key = "bindings" if node_dict.get("type") == "workflow" else "constants"
        node_dict.setdefault(key, {})[field] = serialize_constant(value)

        if self._workflow_cache is not None:
            built = self._workflow_cache._nodes.get(node)
            if built is not None:
                from bioimageflow.workflow_node import WorkflowNode

                if isinstance(built, WorkflowNode):
                    built._input_constant_bindings[field] = value
                    built.workflow._apply_interface_binding(field, value)
                else:
                    built._constant_bindings[field] = value
        self._invalidate_compute_caches()

    def set_enabled(self, node: str, enabled: bool) -> None:
        """Toggle a node's enabled flag.

        Non-structural: the cached workflow is updated in place.
        """
        node_dict = self._get_node_dict(node)
        if enabled:
            node_dict.pop("enabled", None)
        else:
            node_dict["enabled"] = False

        if self._workflow_cache is not None:
            built = self._workflow_cache._nodes.get(node)
            if built is not None:
                built.enabled = enabled
        self._invalidate_compute_caches()

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

        Cached across non-structural edits.
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
