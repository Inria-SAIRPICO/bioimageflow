"""Selected-provider cache identity and planning diagnostics."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .common import (
    Any,
    Node,
    ProcessingTool,
    Storage,
    canonical_dataframe_digest,
    cast,
    compute_env_hash,
    compute_signature_hash,
    get_output_templates,
    get_source_hash,
    get_tool_version,
    pd,
)
from .provenance import (
    ProvenanceRecipe,
    column_provenance_recipe,
    dataframe_provenance_recipe,
    resolve_provenance_recipe,
)


class _IdentityRuntimeMixin:
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
            pointer = Storage(workflow.storage_path).load_current(result_key)
            if pointer is None:
                return None
            return {
                "node_key": provider.name,
                "result_key": result_key,
                "record_id": pointer.record_id,
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
    ) -> str:
        """Compute the logical digest for any node type."""
        tool_version = get_tool_version(node.tool)
        source_hash = get_source_hash(type(node.tool)) if workflow._dev_mode else None
        return compute_signature_hash(
            type(node.tool).__name__,
            tool_version,
            env_hash,
            resolved_params,
            upstream_hashes,
            source_hash=source_hash,
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
        self._normalize_path_arguments(signature_constants, input_annotations)
        signature_defaults = {
            field: getattr(node.tool.Inputs, field)
            for field in input_annotations
            if field not in node._column_bindings
            and field not in node._constant_bindings
            and hasattr(node.tool.Inputs, field)
        }
        self._normalize_path_arguments(signature_defaults, input_annotations)
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
            "defaults": signature_defaults,
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
            )

        raise TypeError(f"Unsupported node type: {type(node.tool).__name__}")
