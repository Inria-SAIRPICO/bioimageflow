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
from typing import Any

from bioimageflow.engine import NodePlan
from bioimageflow.validation import ValidationError, serialize_constant
from bioimageflow.workflow import Workflow


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
        registry: Any | None = None,
        storage_path: str | None = None,
    ) -> None:
        if data is None:
            data = {"nodes": [], "edges": [], "config": {}}
        else:
            data = deepcopy(data)
        if storage_path is not None:
            data.setdefault("config", {})["storage_path"] = storage_path

        self._data: dict[str, Any] = data
        self._registry = registry

        # Cached materialization. `_workflow_cache` is invalidated on
        # structural edits; non-structural edits (constants, enabled
        # flag) update the cached workflow in place to avoid tool
        # re-resolution.
        self._workflow_cache: Workflow | None = None
        self._validate_cache: list[ValidationError] | None = None
        self._plan_cache: dict[str, NodePlan] | None = None

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
        self._plan_cache = None

    def _invalidate_compute_caches(self) -> None:
        # Edits that don't require a workflow rebuild still invalidate
        # the validate/plan caches.
        self._validate_cache = None
        self._plan_cache = None

    # ------------------------------------------------------------------
    # Mutating operations
    # ------------------------------------------------------------------

    def add_node(self, node: dict[str, Any]) -> None:
        """Append a node entry to the session.

        ``node`` is a wire-format dict with at least ``name`` and tool
        identification keys (``tool_module`` + ``tool_class``, or the
        sub-workflow equivalents).
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
            if e.get("from") != name and e.get("to") != name
        ]
        self._invalidate_structural()

    def add_edge(self, edge: dict[str, Any]) -> None:
        """Append an edge entry. Must include ``from``, ``to``, ``column``, ``field``."""
        for key in ("from", "to", "column", "field"):
            if key not in edge:
                raise ValueError(f"Edge missing required key '{key}'.")
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
        node_dict.setdefault("constants", {})[field] = serialize_constant(value)

        if self._workflow_cache is not None:
            built = self._workflow_cache._nodes.get(node)
            if built is not None:
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
        """Return the cached :meth:`Workflow.plan` for the current state."""
        if self._plan_cache is not None:
            return dict(self._plan_cache)
        wf = self.to_workflow()
        plan = wf.plan()
        self._plan_cache = plan
        return dict(plan)

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        registry: Any | None = None,
    ) -> "WorkflowSession":
        return cls(data, registry=registry)
