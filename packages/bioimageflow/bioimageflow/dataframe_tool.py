"""DataFrameTool base class — main process only."""

from typing import Any

from bioimageflow_core.tool import BaseTool, IOModel


class Passthrough(IOModel):
    """Marker base class for DataFrameTool Outputs that preserve input columns."""
    pass


class DataFrameTool(BaseTool):
    """Tool that transforms DataFrames in the main process."""

    accepts_upstream: bool = True
    """Whether this tool accepts positional upstream Nodes.

    Set to ``False`` on source tools (e.g. ``Files``, ``Generate``) that
    produce a DataFrame from constants alone. A source tool raises
    :class:`SourceToolUpstreamError` if any positional argument is passed.
    """

    def __call__(
        self,
        *upstream_nodes: Any,
        name: str | None = None,
        output_templates: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Create a graph node. No computation occurs."""
        if not self.accepts_upstream and upstream_nodes:
            from bioimageflow.node import SourceToolUpstreamError
            raise SourceToolUpstreamError(
                f"Tool '{type(self).__name__}' is a source tool "
                f"(accepts_upstream=False) and does not accept upstream "
                f"DataFrames. Got {len(upstream_nodes)} positional argument(s)."
            )
        try:
            from bioimageflow.node import Node
        except ImportError:
            raise RuntimeError(
                f"{type(self).__name__}.__call__() requires the bioimageflow "
                f"orchestrator package."
            )
        return Node(
            tool=self,
            args=list(upstream_nodes),
            kwargs=kwargs,
            name=name,
            output_templates=output_templates,
        )

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        """Default: inner join on index."""
        if not dfs:
            import pandas as pd
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0]
        for df in dfs[1:]:
            result = result.join(df, how="inner", rsuffix="__bif_dup")
            result = result[[c for c in result.columns if not c.endswith("__bif_dup")]]
        return result

    def transform(self, df: Any, arguments: Any) -> Any:
        """Default: identity (passthrough)."""
        return df

    @classmethod
    def resolve_outputs(
        cls,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]] | None:
        """Return the output column schema for the given (possibly partial) inputs.

        Each value has the per-field shape produced by
        :func:`~bioimageflow.validation.serialize_output_schema`:
        ``{"type": str, "default": Any | None, "image_spec": dict | None}``.

        Default implementation:

        - ``Outputs`` declared as :class:`~bioimageflow_core.IOModel` →
          returns the static schema.
        - ``Outputs`` is a :class:`Passthrough` subclass → returns the marker
          dict ``{"_passthrough": True, **declared_extra_fields}``.
        - Otherwise → returns ``None`` (schema unknown without input information).

        Tools whose output column names are derived from their inputs (e.g.
        ``Generate``, where ``column_name`` is a runtime parameter) override
        this method. Merge tools whose output schema depends on upstream
        schemas (rather than just inputs) instead override
        :meth:`resolve_merge_schema`.

        Must be pure (no I/O, no side effects). Returning ``None`` or a
        partial dict is allowed when ``inputs`` is incomplete.
        """
        from bioimageflow.validation import serialize_output_schema

        outputs_cls = getattr(cls, "Outputs", None)
        if outputs_cls is None:
            return None
        # Delegates to the canonical wire-format helper. Passthrough classes
        # produce ``{"_passthrough": True, ...}`` markers identically to
        # ``serialize_output_schema``.
        return serialize_output_schema(cls)

    @classmethod
    def resolve_merge_schema(
        cls,
        upstream_schemas: list[dict[str, dict[str, Any]] | None],
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]] | None:
        """Return the merged output schema given upstream schemas.

        Override this on merge tools whose output columns are computed from
        upstream column sets (``InnerJoin``, ``CrossJoin``, ``JoinOnColumn``,
        ``Concat``, ``Collect``).

        Returns ``None`` when any required upstream schema is ``None`` (caller
        should treat that as "schema unresolvable"). The default implementation
        returns ``None`` so :meth:`Node.get_output_schema` falls back to
        :meth:`resolve_outputs` for non-merge tools.

        Each entry has the same shape as the values returned by
        :meth:`resolve_outputs`.
        """
        return None
