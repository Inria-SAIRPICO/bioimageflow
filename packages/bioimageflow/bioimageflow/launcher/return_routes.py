"""Exact public-output to executed-provider routes for submitted returns."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from bioimageflow.dataframe_tool import DataFrameTool, Passthrough
from bioimageflow.node import Node
from bioimageflow.validation import is_path_type
from bioimageflow.workflow_node import WorkflowNode
from bioimageflow_core.types import SharedArray

from .inputs import LoadedInvocation


@dataclass(frozen=True, slots=True)
class ReturnProviderRoute:
    """One public return column backed by one exact execution outcome."""

    mapping_key: str | None
    public_column: str
    node_key: str
    provider_column: str
    result_key: str | None
    record_id: str | None
    transient_invocation_id: str | None
    owned: bool
    shared_array: bool


@dataclass(frozen=True, slots=True)
class DeclaredReturnColumn:
    """One typed public column that must receive locator-backed cells."""

    mapping_key: str | None
    public_column: str
    path: bool
    shared_array: bool


@dataclass(frozen=True, slots=True)
class ReturnRoutePlan:
    """Provider candidates plus the complete typed public-column contract."""

    routes: tuple[ReturnProviderRoute, ...]
    declared_columns: tuple[DeclaredReturnColumn, ...]

    def __iter__(self) -> Iterator[ReturnProviderRoute]:
        return iter(self.routes)


@dataclass(slots=True)
class _NodeRoutes:
    routes: dict[str, tuple[ReturnProviderRoute, ...]]
    declared: dict[str, tuple[bool, bool]]


_LINEAGE_KEY = "__bioimageflow_return_route_tokens__"
_PATH_SCHEMA_TYPES = frozenset({"ImageFile", "Path"})
_SHARED_SCHEMA_TYPES = frozenset({"ImageShared"})


def _shared_array_type(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _shared_array_type(get_args(annotation)[0])
    if origin in {Union, UnionType}:
        return any(
            _shared_array_type(value)
            for value in get_args(annotation)
            if value is not type(None)
        )
    return annotation is SharedArray


def _annotation_kinds(annotation: Any) -> tuple[bool, bool]:
    return is_path_type(annotation), _shared_array_type(annotation)


def _schema_kinds(value: Any) -> tuple[bool, bool]:
    if not isinstance(value, Mapping):
        return False, False
    display_type = value.get("type")
    return (
        display_type in _PATH_SCHEMA_TYPES,
        display_type in _SHARED_SCHEMA_TYPES,
    )


def _merge_kinds(
    first: tuple[bool, bool],
    second: tuple[bool, bool],
) -> tuple[bool, bool]:
    return first[0] or second[0], first[1] or second[1]


def _unique_routes(
    routes: list[ReturnProviderRoute],
) -> tuple[ReturnProviderRoute, ...]:
    return tuple(dict.fromkeys(routes))


def _public_route(
    route: ReturnProviderRoute,
    *,
    mapping_key: str | None,
    public_column: str,
) -> ReturnProviderRoute:
    return replace(
        route,
        mapping_key=mapping_key,
        public_column=public_column,
    )


def _node_key(node: Node, scope: tuple[str, ...]) -> str:
    return "/".join((*scope, node._name))


def _outcome_route(
    node: Node,
    column: str,
    *,
    scope: tuple[str, ...],
    outcomes: Mapping[str, Any],
    path: bool,
    shared_array: bool,
) -> ReturnProviderRoute | None:
    node_key = _node_key(node, scope)
    outcome = outcomes.get(node_key)
    if outcome is None:
        return None
    return ReturnProviderRoute(
        mapping_key=None,
        public_column=column,
        node_key=node_key,
        provider_column=column,
        result_key=outcome.result_key,
        record_id=outcome.record_id,
        transient_invocation_id=outcome.transient_invocation_id,
        owned=column in outcome.owned_path_columns,
        shared_array=shared_array or column in outcome.shared_array_columns,
    )


def _workflow_output_schema(node: WorkflowNode) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for port in node.workflow._interface_outputs.values():
        if isinstance(port.schema, Mapping):
            result[port.name] = dict(port.schema)
        else:
            path, shared = _annotation_kinds(port.annotation)
            display_type = (
                "ImageShared" if shared and not path else "Path" if path else "any"
            )
            result[port.name] = {
                "type": display_type,
                "default": None,
                "image_spec": None,
            }
    return result


def _node_schema(node: Node) -> dict[str, dict[str, Any]]:
    if isinstance(node, WorkflowNode):
        return _workflow_output_schema(node)
    schema = node.get_output_schema()
    if not isinstance(schema, Mapping) or schema.get("_passthrough") is True:
        return {}
    return {
        str(column): dict(value)
        for column, value in schema.items()
        if isinstance(column, str) and isinstance(value, Mapping)
    }


def _workflow_routes(
    node: WorkflowNode,
    *,
    scope: tuple[str, ...],
    outcomes: Mapping[str, Any],
    memo: dict[tuple[int, tuple[str, ...]], _NodeRoutes],
) -> _NodeRoutes:
    nested_scope = (*scope, node._name)
    routes: dict[str, tuple[ReturnProviderRoute, ...]] = {}
    declared: dict[str, tuple[bool, bool]] = {}
    for port in node.workflow._interface_outputs.values():
        source = node.workflow._nodes[port.source_node]
        selected = _column_routes(
            source,
            port.source_output,
            scope=nested_scope,
            outcomes=outcomes,
            memo=memo,
        )
        routes[port.name] = selected
        kinds = _annotation_kinds(port.annotation)
        if kinds == (False, False):
            for route in selected:
                kinds = _merge_kinds(
                    kinds,
                    (not route.shared_array, route.shared_array),
                )
        if kinds != (False, False):
            declared[port.name] = kinds
    return _NodeRoutes(routes=routes, declared=declared)


def _lineage_schema(
    node: Node,
    routes: _NodeRoutes,
    tokens: dict[str, ReturnProviderRoute],
) -> dict[str, dict[str, Any]]:
    schema = _node_schema(node)
    columns = set(schema) | set(routes.routes)
    result: dict[str, dict[str, Any]] = {}
    for column in columns:
        entry = dict(
            schema.get(
                column,
                {"type": "any", "default": None, "image_spec": None},
            )
        )
        route_tokens: list[str] = []
        for route in routes.routes.get(column, ()):
            token = f"route_{len(tokens):08d}"
            tokens[token] = route
            route_tokens.append(token)
        if route_tokens:
            entry[_LINEAGE_KEY] = route_tokens
        result[column] = entry
    return result


def _dataframe_routes(
    node: Node,
    *,
    scope: tuple[str, ...],
    outcomes: Mapping[str, Any],
    memo: dict[tuple[int, tuple[str, ...]], _NodeRoutes],
) -> _NodeRoutes:
    key = (id(node), scope)
    existing = memo.get(key)
    if existing is not None:
        return existing

    if isinstance(node, WorkflowNode):
        result = _workflow_routes(
            node,
            scope=scope,
            outcomes=outcomes,
            memo=memo,
        )
        memo[key] = result
        return result

    outcome = outcomes.get(_node_key(node, scope))
    if not isinstance(node.tool, DataFrameTool):
        routes: dict[str, tuple[ReturnProviderRoute, ...]] = {}
        declared: dict[str, tuple[bool, bool]] = {}
        if outcome is not None:
            path_columns = set(outcome.path_columns)
            shared_columns = set(outcome.shared_array_columns)
            for column in sorted(path_columns | shared_columns):
                route = _outcome_route(
                    node,
                    column,
                    scope=scope,
                    outcomes=outcomes,
                    path=column in path_columns,
                    shared_array=column in shared_columns,
                )
                if route is not None:
                    routes[column] = (route,)
                    declared[column] = (
                        column in path_columns,
                        column in shared_columns,
                    )
        result = _NodeRoutes(routes=routes, declared=declared)
        memo[key] = result
        return result

    upstream_nodes = [
        argument for argument in node._args if isinstance(argument, Node)
    ]
    upstream = [
        _dataframe_routes(
            argument,
            scope=scope,
            outcomes=outcomes,
            memo=memo,
        )
        for argument in upstream_nodes
    ]
    outputs = getattr(node.tool, "Outputs", None)
    passthrough = (
        isinstance(outputs, type)
        and issubclass(outputs, Passthrough)
    )
    output_schema = _node_schema(node)

    candidate_lists: dict[str, list[ReturnProviderRoute]] = {}
    declared: dict[str, tuple[bool, bool]] = {
        column: kinds
        for column, entry in output_schema.items()
        if (kinds := _schema_kinds(entry)) != (False, False)
    }

    if upstream_nodes:
        tokens: dict[str, ReturnProviderRoute] = {}
        lineage_schemas: list[dict[str, dict[str, Any]] | None] = [
            _lineage_schema(argument, routes, tokens)
            for argument, routes in zip(upstream_nodes, upstream)
        ]
        try:
            resolved = type(node.tool).resolve_merge_schema(
                lineage_schemas,
                node._constant_bindings,
            )
        except Exception:
            resolved = None
        if isinstance(resolved, Mapping):
            for column, entry in resolved.items():
                if not isinstance(column, str) or not isinstance(entry, Mapping):
                    continue
                for token in entry.get(_LINEAGE_KEY, ()):
                    route = tokens.get(token)
                    if route is not None:
                        candidate_lists.setdefault(column, []).append(route)

        output_names = (
            set(output_schema)
            if output_schema
            else set(resolved) if isinstance(resolved, Mapping)
            else set().union(*(set(item.routes) for item in upstream))
        )
        schema_is_resolved = bool(output_schema) or isinstance(resolved, Mapping)
        for column in output_names:
            if (
                schema_is_resolved
                and column not in declared
                and not candidate_lists.get(column)
            ):
                continue
            for item in upstream:
                candidate_lists.setdefault(column, []).extend(
                    item.routes.get(column, ())
                )
                if column in item.declared:
                    declared[column] = _merge_kinds(
                        declared.get(column, (False, False)),
                        item.declared[column],
                    )

    if not upstream_nodes or (not passthrough and outputs is not None):
        path_columns = set(getattr(outcome, "path_columns", ()))
        shared_columns = set(getattr(outcome, "shared_array_columns", ()))
        for column, entry in output_schema.items():
            path, shared = _schema_kinds(entry)
            if not (path or shared):
                continue
            route = _outcome_route(
                node,
                column,
                scope=scope,
                outcomes=outcomes,
                path=path,
                shared_array=shared,
            )
            if route is not None:
                candidate_lists.setdefault(column, []).append(route)
        for column in sorted(path_columns | shared_columns):
            route = _outcome_route(
                node,
                column,
                scope=scope,
                outcomes=outcomes,
                path=column in path_columns,
                shared_array=column in shared_columns,
            )
            if route is not None:
                candidate_lists.setdefault(column, []).append(route)
                declared[column] = _merge_kinds(
                    declared.get(column, (False, False)),
                    (column in path_columns, column in shared_columns),
                )

    routes = {
        column: _unique_routes(values)
        for column, values in candidate_lists.items()
        if values
    }
    for column, values in routes.items():
        for route in values:
            declared[column] = _merge_kinds(
                declared.get(column, (False, False)),
                (not route.shared_array, route.shared_array),
            )
    result = _NodeRoutes(routes=routes, declared=declared)
    memo[key] = result
    return result


def _column_routes(
    node: Node,
    column: str,
    *,
    scope: tuple[str, ...],
    outcomes: Mapping[str, Any],
    memo: dict[tuple[int, tuple[str, ...]], _NodeRoutes],
) -> tuple[ReturnProviderRoute, ...]:
    if isinstance(node, WorkflowNode):
        port = node.workflow._interface_outputs.get(column)
        if port is None:
            return ()
        source = node.workflow._nodes[port.source_node]
        return _column_routes(
            source,
            port.source_output,
            scope=(*scope, node._name),
            outcomes=outcomes,
            memo=memo,
        )
    return _dataframe_routes(
        node,
        scope=scope,
        outcomes=outcomes,
        memo=memo,
    ).routes.get(column, ())


def build_return_provider_routes(
    workflow: Any,
    invocation: LoadedInvocation,
    outcomes: tuple[Any, ...],
) -> ReturnRoutePlan:
    """Resolve every typed public column to exact provider candidates."""
    by_node = {outcome.node_key: outcome for outcome in outcomes}
    memo: dict[tuple[int, tuple[str, ...]], _NodeRoutes] = {}
    routes: list[ReturnProviderRoute] = []
    declared: list[DeclaredReturnColumn] = []

    if invocation.variant == "root":
        for port in workflow._interface_outputs.values():
            source = workflow.nodes[port.source_node]
            selected = _column_routes(
                source,
                port.source_output,
                scope=(),
                outcomes=by_node,
                memo=memo,
            )
            routes.extend(
                _public_route(
                    route,
                    mapping_key=None,
                    public_column=port.name,
                )
                for route in selected
            )
            path, shared = _annotation_kinds(port.annotation)
            if selected:
                path = path or any(not route.shared_array for route in selected)
                shared = shared or any(route.shared_array for route in selected)
            if path or shared:
                declared.append(
                    DeclaredReturnColumn(
                        mapping_key=None,
                        public_column=port.name,
                        path=path,
                        shared_array=shared,
                    )
                )
        return ReturnRoutePlan(tuple(routes), tuple(declared))

    multiple = len(invocation.targets) > 1
    for target_name in invocation.targets:
        target = workflow.nodes[target_name]
        mapping_key = target_name if multiple else None
        node_routes = _dataframe_routes(
            target,
            scope=(),
            outcomes=by_node,
            memo=memo,
        )
        for public_column, selected in node_routes.routes.items():
            routes.extend(
                _public_route(
                    route,
                    mapping_key=mapping_key,
                    public_column=public_column,
                )
                for route in selected
            )
        for public_column, (path, shared) in node_routes.declared.items():
            if path or shared:
                declared.append(
                    DeclaredReturnColumn(
                        mapping_key=mapping_key,
                        public_column=public_column,
                        path=path,
                        shared_array=shared,
                    )
                )
    return ReturnRoutePlan(tuple(routes), tuple(declared))
