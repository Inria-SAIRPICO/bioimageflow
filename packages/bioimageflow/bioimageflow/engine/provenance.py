"""Static provenance recipes for values consumed across workflow boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class WorkflowOutputSelector:
    """One stable workflow-output hop on the path to a real provider."""

    boundary: Any
    output_id: str


@dataclass(frozen=True)
class PublishedProviderRecipe:
    """A real tool provider and the selectors needed to reach its value."""

    provider: Any
    provider_output: str | None
    workflow_outputs: tuple[WorkflowOutputSelector, ...] = ()
    assembled_output_id: str | None = None
    assembled_output_name: str | None = None


@dataclass(frozen=True)
class ProvenanceRecipe:
    """The selected-provider recipe for one consumed column or dataframe."""

    kind: str
    providers: tuple[PublishedProviderRecipe, ...]


def column_provenance_recipe(node: Any, column: str) -> ProvenanceRecipe:
    """Return the real-provider recipe for one consumed output column."""
    provider = _column_provider_recipe(node, column)
    return ProvenanceRecipe(kind="column", providers=(provider,))


def dataframe_provenance_recipe(node: Any) -> ProvenanceRecipe:
    """Return the ordered real-provider recipe for a consumed dataframe."""
    from bioimageflow.workflow_node import WorkflowNode

    if not isinstance(node, WorkflowNode):
        return ProvenanceRecipe(
            kind="dataframe",
            providers=(
                PublishedProviderRecipe(
                    provider=node,
                    provider_output=None,
                ),
            ),
        )

    providers: list[PublishedProviderRecipe] = []
    for port in node.workflow._interface_outputs.values():
        source = node.workflow._nodes[port.source_node]
        provider = _column_provider_recipe(source, port.source_output)
        providers.append(
            PublishedProviderRecipe(
                provider=provider.provider,
                provider_output=provider.provider_output,
                workflow_outputs=(
                    WorkflowOutputSelector(node, port.id),
                    *provider.workflow_outputs,
                ),
                assembled_output_id=port.id,
                assembled_output_name=port.name,
            )
        )
    return ProvenanceRecipe(kind="workflow_dataframe", providers=tuple(providers))


def resolve_provenance_recipe(
    recipe: ProvenanceRecipe,
    select_provider: Callable[[Any], dict[str, str] | None],
) -> dict[str, Any] | None:
    """Resolve a recipe to selected immutable records, or return ``None``."""
    resolved: list[dict[str, Any]] = []
    for provider in recipe.providers:
        selection = select_provider(provider.provider)
        if selection is None:
            return None
        selector: dict[str, Any] = {
            "workflow_outputs": [
                {
                    "boundary_key": item.boundary.name,
                    "output_id": item.output_id,
                }
                for item in provider.workflow_outputs
            ],
        }
        if provider.provider_output is None:
            selector["provider_value"] = {"kind": "dataframe"}
        else:
            selector["provider_value"] = {
                "kind": "column",
                "output": provider.provider_output,
            }
        item: dict[str, Any] = {
            "provider": selection,
            "selector": selector,
        }
        if provider.assembled_output_id is not None:
            item["assembled_output"] = {
                "id": provider.assembled_output_id,
                "name": provider.assembled_output_name,
            }
        resolved.append(item)
    return {
        "kind": recipe.kind,
        "providers": resolved,
    }


def _column_provider_recipe(node: Any, column: str) -> PublishedProviderRecipe:
    from bioimageflow.workflow_node import WorkflowNode

    if not isinstance(node, WorkflowNode):
        return PublishedProviderRecipe(
            provider=node,
            provider_output=column,
        )

    port = node.workflow._interface_outputs.get(column)
    if port is None:
        raise ValueError(
            f"Workflow boundary '{node.name}' has no output ID {column!r}."
        )
    source = node.workflow._nodes[port.source_node]
    provider = _column_provider_recipe(source, port.source_output)
    return PublishedProviderRecipe(
        provider=provider.provider,
        provider_output=provider.provider_output,
        workflow_outputs=(
            WorkflowOutputSelector(node, port.id),
            *provider.workflow_outputs,
        ),
    )
