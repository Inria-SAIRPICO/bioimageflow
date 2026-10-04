"""Explicit result-row domains and consumed-observation associations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from bioimageflow_core.worker_protocol import ConsumedRow


@dataclass(frozen=True)
class RowAssociation:
    """One output group and its ordered, actual consumed observations."""

    consumed_rows: tuple[ConsumedRow, ...]
    output_indices: tuple[str, ...]


@dataclass(frozen=True)
class ResultRelation:
    """Owned relation metadata, separate from DataFrame/index contents."""

    row_consumption: str
    output_domain: str
    domain_kind: str
    row_associations: tuple[RowAssociation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.row_consumption, str) or self.row_consumption not in {"mapped", "collective", "dataframe"}:
            raise ValueError("Invalid result row consumption.")
        if not isinstance(self.domain_kind, str) or self.domain_kind not in {"source", "aggregate", "merge"}:
            raise ValueError("Invalid result domain kind.")
        if not isinstance(self.output_domain, str) or not self.output_domain:
            raise ValueError("A result relation requires an explicit output domain.")
        if not isinstance(self.row_associations, tuple):
            raise ValueError("Result associations must be an immutable tuple.")
        if self.row_consumption == "collective" and len(self.row_associations) != 1:
            raise ValueError("Collective relations require one all-consumed group.")
        outputs: set[str] = set()
        mapped_positions: list[int] = []
        for group in self.row_associations:
            if not isinstance(group, RowAssociation) or not isinstance(group.consumed_rows, tuple) or not isinstance(group.output_indices, tuple):
                raise ValueError("Result groups must own immutable row/index tuples.")
            previous_position = -1
            for row in group.consumed_rows:
                if type(row.position) is not int or row.position < 0 or not isinstance(row.row_index, str):
                    raise ValueError("Invalid consumed-row identity.")
                if row.position <= previous_position:
                    raise ValueError("Consumed-row positions must be unique and increasing.")
                previous_position = row.position
            if self.row_consumption == "mapped":
                if len(group.consumed_rows) != 1:
                    raise ValueError("Mapped relations require singleton consumption groups.")
                mapped_positions.append(group.consumed_rows[0].position)
            for index in group.output_indices:
                if not isinstance(index, str) or index in outputs:
                    raise ValueError("Output row identities must be unique strings.")
                outputs.add(index)
        if mapped_positions != sorted(set(mapped_positions)):
            raise ValueError("Mapped relation groups must have unique increasing positions.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_consumption": self.row_consumption,
            "output_domain": self.output_domain,
            "domain_kind": self.domain_kind,
            "groups": [
                {
                    "consumed_rows": [
                        {"position": row.position, "row_index": row.row_index}
                        for row in group.consumed_rows
                    ],
                    "output_indices": list(group.output_indices),
                }
                for group in self.row_associations
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ResultRelation:
        if not isinstance(value, dict) or set(value) != {"row_consumption", "output_domain", "domain_kind", "groups"}:
            raise ValueError("Invalid result relation fields.")
        if not isinstance(value["groups"], list):
            raise ValueError("Result relation groups must be a list.")
        groups: list[RowAssociation] = []
        for group in value["groups"]:
            if not isinstance(group, dict) or set(group) != {"consumed_rows", "output_indices"}:
                raise ValueError("Invalid result association fields.")
            if not isinstance(group["consumed_rows"], list) or not isinstance(group["output_indices"], list):
                raise ValueError("Result associations require row/index lists.")
            consumed: list[ConsumedRow] = []
            for row in group["consumed_rows"]:
                if not isinstance(row, dict) or set(row) != {"position", "row_index"}:
                    raise ValueError("Invalid consumed-row fields.")
                consumed.append(ConsumedRow(position=row["position"], row_index=row["row_index"]))
            groups.append(RowAssociation(tuple(consumed), tuple(group["output_indices"])))
        return cls(value["row_consumption"], value["output_domain"], value["domain_kind"], tuple(groups))


@dataclass(frozen=True)
class AssembledResult:
    dataframe: pd.DataFrame
    relation: ResultRelation
