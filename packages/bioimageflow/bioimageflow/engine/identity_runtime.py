"""Selected-provider cache identity and planning diagnostics."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Node,
    ProcessingTool,
    canonical_dataframe_digest,
    cast,
    compute_env_hash,
    compute_signature_hash,
    get_output_templates,
    pd,
)
from .provenance import (
    ProvenanceRecipe,
    column_provenance_recipe,
    dataframe_provenance_recipe,
    resolve_provenance_recipe,
)


class _IdentityRuntimeMixin:
    def _admit_node_runtime(self, node: Node, *, provision: bool) -> Any:
        """Share one ready-content admission per augmented recipe and operation."""
        if not self._use_wetlands or not isinstance(node.tool, ProcessingTool):
            return None
        assert self._env_manager is not None
        receipt = self._env_manager.admit_runtime(
            node.tool.environment, provision=provision,
            admissions=self._runtime_admissions,
        )
        self._node_runtime_receipts[node] = receipt
        return receipt

    def _validate_node_runtime(self, node: Node) -> None:
        if not self._use_wetlands or not isinstance(node.tool, ProcessingTool):
            return
        receipt = self._node_runtime_receipts.get(node)
        if receipt is None:
            raise RuntimeError("Managed Processing requires an admitted ready runtime")
        self._env_manager.validate_runtime_receipt(node.tool.environment, receipt)

    def _runtime_identity(self, node: Node) -> dict[str, Any] | None:
        receipt = self._node_runtime_receipts.get(node)
        if receipt is None:
            return None
        return {
            "content_digest": receipt.content_digest,
            "facts": receipt.to_scientific_facts(),
        }

    def _capture_executable(self, node: Node) -> Any:
        """Own one admission per operation, shared by lookup and actual call."""
        from bioimageflow.worker_origins import capture_tool_executable
        from bioimageflow.cache.identity import deterministic_serialize

        with self._cache_hit_lock:
            capture = self._node_executable_captures.get(node)
            if capture is None:
                capture = capture_tool_executable(
                    node.tool, managed=self._use_wetlands and isinstance(node.tool, ProcessingTool),
                    canonicalize=deterministic_serialize,
                    metadata=self._executable_metadata,
                )
                self._node_executable_captures[node] = capture
            return capture

    def _upstream_identity_map(
        self,
        workflow: Any,
        bindings: list[tuple[str, ProvenanceRecipe]],
        sig_hashes: dict[Node, str | None],
    ) -> dict[str, Any] | None:
        """Resolve consumed values to selected real-provider records."""

        def select_provider(provider: Node) -> dict[str, str] | None:
            sig_hash = sig_hashes.get(provider)
            if sig_hash is None:
                return None
            result_key = self._node_result_key(provider, sig_hash)
            if result_key is None:
                return None
            selection = self._selected_result(provider)
            if selection is None or selection.result_key != result_key:
                return None
            return {
                "node_key": provider.name,
                "result_key": result_key,
                "record_id": selection.record_id,
            }

        identities: dict[str, Any] = {}
        for binding, recipe in bindings:
            resolved = resolve_provenance_recipe(recipe, select_provider)
            if resolved is None:
                return None
            identities[binding] = resolved
        return identities

    def _dataframe_upstream_recipes(
        self,
        node: Node,
    ) -> list[tuple[str, ProvenanceRecipe]]:
        """Return selector-aware recipes for positional dataframe inputs."""
        return [
            (f"argument_{index}", dataframe_provenance_recipe(argument))
            for index, argument in enumerate(node._args)
            if isinstance(argument, Node)
        ]

    def _processing_upstream_recipes(
        self,
        node: Node,
    ) -> list[tuple[str, ProvenanceRecipe]]:
        """Return selector-aware recipes for named processing inputs."""
        return [
            (field, column_provenance_recipe(reference.node, reference.column))
            for field, reference in sorted(node._column_bindings.items())
        ]

    def _compute_sig_hash(
        self,
        node: Node,
        env_hash: str,
        resolved_params: Any,
        upstream_hashes: dict[str, Any],
        workflow: Any,
        *,
        diagnostic: bool = False,
    ) -> str:
        """Compute the logical digest for any node type."""
        capture = self._capture_executable(node)
        from bioimageflow.cache.identity import deterministic_serialize
        tool_facts = dict(capture.scientific_key)
        if self._use_wetlands and isinstance(node.tool, ProcessingTool):
            receipt = self._node_runtime_receipts.get(node)
            if receipt is None:
                if not diagnostic:
                    raise RuntimeError("Managed signature requires an admitted ready runtime")
                tool_facts["managed_runtime"] = {"pending": True}
            else:
                tool_facts["managed_runtime"] = self._runtime_identity(node)
        tool_version = deterministic_serialize(tool_facts)
        from bioimageflow.portable_cells import portable_identity
        from enum import Enum

        def field_identity(value: Any) -> Any:
            # Declared Enum choices keep their existing primitive-value semantics.
            return portable_identity(value.value if isinstance(value, Enum) else value)

        if isinstance(node.tool, ProcessingTool):
            resolved_params = dict(resolved_params)
            for category in ("constants", "defaults"):
                if category in resolved_params:
                    resolved_params[category] = {
                        name: field_identity(value)
                        for name, value in resolved_params[category].items()
                    }
            resolved_params = {
                "arguments": resolved_params,
                "row_consumption": node.tool.row_consumption.value,
                "collective_reference_inputs": list(node.tool.collective_reference_inputs),
            }
        else:
            declared_fields = node.tool.Inputs._get_all_annotations()
            resolved_params = {
                name: field_identity(value) if name in declared_fields else value
                for name, value in resolved_params.items()
            }
        return compute_signature_hash(
            type(node.tool).__name__,
            tool_version,
            env_hash,
            resolved_params,
            upstream_hashes,
        )

    def _compute_processing_sig_hash(
        self,
        node: Node,
        input_annotations: dict[str, Any],
        upstream_nodes: dict[str, Node],
        sig_hashes: dict[Node, str | None],
        workflow: Any,
    ) -> str | None:
        """Compute the logical digest for a non-source ProcessingTool."""
        env_hash = compute_env_hash(
            cast(ProcessingTool, node.tool).environment.dependencies
        )
        assert node.tool.Outputs is not None
        missing = [n.name for n in upstream_nodes.values() if n not in sig_hashes]
        if missing:
            raise RuntimeError(
                f"Cannot compute logical digest for node: upstream nodes "
                f"{missing} have not been executed yet."
            )
        upstream_hash_map = self._upstream_identity_map(
            workflow,
            self._processing_upstream_recipes(node),
            sig_hashes,
        )
        if upstream_hash_map is None:
            return None
        resolved_params = self._processing_signature_params(
            node,
            input_annotations,
        )
        return self._compute_sig_hash(
            node,
            env_hash,
            resolved_params,
            upstream_hash_map,
            workflow,
        )

    def _processing_signature_params(
        self,
        node: Node,
        input_annotations: dict[str, Any],
    ) -> dict[str, Any]:
        """Return normalized static processing-node signature material."""
        signature_constants = dict(node._constant_bindings)
        # Execution captures omitted defaults into constants. Read-only planning
        # must give the same effective values the same identity, while a column
        # binding remains authoritative over a declared default.
        for field in input_annotations:
            if field not in node._column_bindings and field not in signature_constants and hasattr(node.tool.Inputs, field):
                signature_constants[field] = getattr(node.tool.Inputs, field)
        self._normalize_path_arguments(signature_constants, input_annotations)
        assert node.tool.Outputs is not None
        return {
            "bindings": {
                field: {
                    "node": reference.node.name,
                    "column": reference.column,
                }
                for field, reference in node._column_bindings.items()
            },
            "constants": signature_constants,
            "defaults": {},
            "output_templates": get_output_templates(
                node.tool.Outputs,
                node.tool.Inputs,
                node.output_templates,
            ),
        }

    def _compute_pending_diagnostic_sig_hash(
        self,
        node: Node,
        diagnostic_hashes: dict[Node, str],
        workflow: Any,
    ) -> str:
        """Compute a non-cache diagnostic signature for unresolved planning."""
        from bioimageflow.dataframe_tool import DataFrameTool

        if isinstance(node.tool, DataFrameTool):
            _arguments, resolved_params = self._resolve_constant_arguments(node)
            for index, argument in enumerate(node._args):
                if isinstance(argument, pd.DataFrame):
                    resolved_params[f"workflow_dataframe_input_{index}"] = (
                        canonical_dataframe_digest(argument)
                    )
            upstream = {
                f"argument_{index}": {
                    "node_key": argument.name,
                    "diagnostic_signature": diagnostic_hashes.get(
                        argument,
                        "pending",
                    ),
                }
                for index, argument in enumerate(node._args)
                if isinstance(argument, Node)
            }
            return self._compute_sig_hash(
                node,
                "",
                resolved_params,
                upstream,
                workflow,
                diagnostic=True,
            )

        if isinstance(node.tool, ProcessingTool):
            input_annotations = node.tool.Inputs._get_all_annotations()
            upstream = {
                field: {
                    "node_key": reference.node.name,
                    "output": reference.column,
                    "diagnostic_signature": diagnostic_hashes.get(
                        reference.node,
                        "pending",
                    ),
                }
                for field, reference in sorted(node._column_bindings.items())
            }
            return self._compute_sig_hash(
                node,
                compute_env_hash(node.tool.environment.dependencies),
                self._processing_signature_params(node, input_annotations),
                upstream,
                workflow,
                diagnostic=True,
            )

        raise TypeError(f"Unsupported node type: {type(node.tool).__name__}")
