"""Public exact-output ownership planning and actionable template diagnostics."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping, Sequence

from bioimageflow_core import FileGroup, PathBase, PlannedFile, Template
from bioimageflow.storage import CacheCorruptionError, validate_relative_posix_path
from bioimageflow.template import resolve_template

_TOKEN = re.compile(r"\{(?:column:\w+|\w+(?:\.\w+)?)\}")
_MAX_EXAMPLES = 8
_MAX_EVIDENCE_BYTES = 2_000_000


def file_group_description(group: FileGroup) -> str:
    if group.kind == "per_row":
        return "One file per input row"
    if group.kind == "once":
        return "One file for the whole batch"
    return "One file per distinct " + ", ".join(group.keys)


def output_file_declarations(tool: Any, overrides: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Public detached role/group metadata from the actual bound declaration."""
    outputs = getattr(tool, "Outputs", None)
    result = {}
    for name in (() if outputs is None else outputs._get_all_annotations()):
        default = getattr(outputs, name, None)
        if not isinstance(default, Template):
            continue
        group = None if isinstance(default, PathBase) else default.group or FileGroup.per_row()
        result[name] = {"field": name, "template": (overrides or {}).get(name) or default.pattern,
                        "kind": "base" if group is None else "exact",
                        "group": None if group is None else group.to_dict(),
                        "description": "Base name; this tool generates the individual filenames" if group is None else file_group_description(group)}
    unknown = {name for name, pattern in (overrides or {}).items() if pattern} - set(result)
    if unknown:
        raise ValueError(f"Returned path references cannot have output templates: {sorted(unknown)}. Declare an intended write with Template(...) or a generated naming family with PathBase(...) in the editable tool definition, or upgrade its incompatible tool package. Remove the override only if this output is a true returned reference.")
    return result


def _scalar(value: Any) -> Any:
    if isinstance(value, Path):
        return value
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    # NumPy scalar input values are normalized without retaining array owners.
    if type(value).__module__.startswith("numpy") and getattr(value, "ndim", None) == 0:
        return _scalar(getattr(value, "item")())
    raise TypeError("File grouping/template evidence supports finite scalar and path values")


def _key(value: Any) -> tuple[str, Any]:
    value = _scalar(value)
    if isinstance(value, Path):
        return ("path", str(value))
    return (type(value).__name__, value.hex() if type(value) is float else value)


def _context(values: Mapping[str, Any]) -> dict[str, Any]:
    captured = {}
    for name, value in values.items():
        if name == "_columns":
            columns = {}
            for column, item in value.items():
                try:
                    columns[column] = _scalar(item)
                except TypeError:
                    pass
            captured[name] = columns
        else:
            try:
                captured[name] = _scalar(value)
            except TypeError:
                pass
    return captured


def _expand(pattern: str, context: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    parts, cursor = [], 0
    for match in _TOKEN.finditer(pattern):
        if match.start() > cursor:
            literal = pattern[cursor:match.start()]
            parts.append({"kind": "literal", "text": literal, "value": literal})
        token = match.group()
        value = resolve_template(token, context)
        if value == token:
            raise ValueError(f"Output template variable {token} is unavailable for this file group")
        parts.append({"kind": "token", "text": token, "value": value})
        cursor = match.end()
    if cursor < len(pattern):
        literal = pattern[cursor:]
        parts.append({"kind": "literal", "text": literal, "value": literal})
    resolved = resolve_template(pattern, context)
    if "{" in resolved or "}" in resolved:
        raise ValueError(f"Unresolved output template: {resolved!r}")
    try:
        return validate_relative_posix_path(Path(resolved).as_posix()), parts
    except ValueError as error:
        raise CacheCorruptionError(f"Unsafe output template path: {resolved!r}") from error


class OutputPlanningError(ValueError):
    """A template conflict with bounded public explanation and optional evidence."""

    def __init__(self, report: dict[str, Any], evidence: OutputPlanningEvidence | None):
        self.report = deepcopy(report)
        self.evidence = evidence
        fields = ", ".join(f"{item['field']}: {item['template']}" for item in report["fields"])
        paths = ", ".join(item["path"] for item in report["conflicts"])
        super().__init__(f"{report['detail']} Node: {report['node_id']}. Templates: {fields}. Destinations: {paths}. "
                         "Change the output template to distinguish separate files; rows contributing to one file must agree on one destination.")


class OutputPathCollisionError(OutputPlanningError):
    """Different independently owned files request the same destination."""


class OutputGroupDestinationMismatchError(OutputPlanningError):
    """Members contributing to one file request different destinations."""


@dataclass(frozen=True)
class OutputFilePlan:
    output_files: dict[str, tuple[PlannedFile, ...]]
    row_paths: tuple[dict[str, str], ...]
    reference_paths: tuple[dict[str, str], ...]
    batch_paths: dict[str, str]

    def validate_outputs(self, groups: Sequence[Any], *, row_consumption: str) -> None:
        """Admit returned exact paths without confusing repeated references with writes."""
        admitted = {field: {item.path: frozenset(item.row_positions) for item in files}
                    for field, files in self.output_files.items()}
        for group in groups:
            positions = {row.position for row in group.consumed_rows}
            for output in group.outputs:
                values = dict(output) if isinstance(output, Mapping) else vars(output)
                for field, owners in admitted.items():
                    if field not in values or not isinstance(values[field], (str, Path)):
                        # Path | SharedArray outputs can choose the memory route;
                        # their potential file remains unused in that case.
                        continue
                    members = owners.get(Path(values[field]))
                    if members is None or (row_consumption != "collective" and not any(position in members for position in positions)):
                        raise ValueError(f"Output {field!r} returned an undeclared exact destination: {values[field]}")

    def manifest_only_artifacts(self, dataframe: Any) -> list[tuple[str, str, Path]]:
        """Publish actual planned files even when a collective returns no records."""
        artifacts = []
        for field, files in self.output_files.items():
            represented = set()
            if field in dataframe.columns:
                represented = {Path(value) for value in dataframe[field] if isinstance(value, (str, Path))}
            for item in files:
                if item.path not in represented and item.path.exists():
                    artifacts.append((field, f"file-group-{item.group_index}", item.path))
        return artifacts


@dataclass(frozen=True)
class OutputPlanningEvidence:
    """Lightweight operation-owned template inputs; no image or array owners."""

    node_id: str
    assets_dir: Path
    _fields: dict[str, dict[str, Any]]
    _rows: tuple[dict[str, Any], ...]
    _references: tuple[dict[str, Any], ...]
    _batch: dict[str, Any]
    _row_indexes: tuple[str, ...]
    _row_consumption: str = "mapped"
    case_sensitive: bool = os.name != "nt"

    @classmethod
    def capture(cls, *, node_id: str, assets_dir: Path, tool: Any,
                row_contexts: Sequence[Mapping[str, Any]], row_indexes: Sequence[str],
                reference_contexts: Sequence[Mapping[str, Any]] = (),
                batch_context: Mapping[str, Any] | None = None,
                templates: Mapping[str, str] | None = None,
                case_sensitive: bool | None = None) -> OutputPlanningEvidence:
        return cls(node_id, assets_dir, output_file_declarations(tool, templates),
                   tuple(_context(row) for row in row_contexts),
                   tuple(_context(row) for row in reference_contexts),
                   _context(batch_context or {}), tuple(str(index) for index in row_indexes),
                   getattr(getattr(tool, "row_consumption", None), "value", "mapped"),
                   _filesystem_case_sensitive(assets_dir) if case_sensitive is None else case_sensitive)

    def check_templates(self, overrides: Mapping[str, str] | None = None) -> OutputFilePlan:
        """Check every captured owner, including newly referenced scalar/path inputs."""
        fields = deepcopy(self._fields)
        for name, pattern in (overrides or {}).items():
            if name not in fields or type(pattern) is not str or not pattern:
                raise ValueError(f"Invalid writable output template override: {name!r}")
            fields[name]["template"] = pattern
        return self._plan(fields, suggestions=True)

    def _plan(self, fields: dict[str, dict[str, Any]], *, suggestions: bool) -> OutputFilePlan:
        row_paths = tuple({} for _ in self._rows)
        reference_paths = tuple({} for _ in self._references)
        batch_paths: dict[str, str] = {}
        output_files: dict[str, tuple[PlannedFile, ...]] = {}
        if self._row_consumption == "mapped" and not self._rows:
            return OutputFilePlan({name: () for name, item in fields.items() if item["kind"] == "exact"},
                                  row_paths, reference_paths, batch_paths)
        destinations: dict[str, list[dict[str, Any]]] = {}
        conflicts = []
        mismatches = []
        owners = 0
        for field, declaration in fields.items():
            pattern = declaration["template"]
            if declaration["kind"] == "base":
                for contexts, paths in ((self._rows, row_paths), (self._references, reference_paths)):
                    for context, path_values in zip(contexts, paths):
                        relative, _ = _expand(pattern, context)
                        path_values[field] = str(self._destination(relative))
                if (not self._rows and not self._references) or all(
                    resolve_template(match.group(), self._batch) != match.group()
                    for match in _TOKEN.finditer(pattern)
                ):
                    relative, _ = _expand(pattern, self._batch)
                    batch_paths[field] = str(self._destination(relative))
                continue
            group = FileGroup(declaration["group"]["kind"], tuple(declaration["group"]["keys"]))
            buckets: OrderedDict[Any, list[tuple[str, int | None, dict[str, Any]]]] = OrderedDict()
            for kind, contexts in (("row", self._rows), ("reference", self._references)):
                if kind == "reference" and group.kind != "by":
                    continue
                for position, context in enumerate(contexts):
                    identity = (kind, position) if group.kind == "per_row" else ("once",)
                    if group.kind == "by":
                        missing = [key for key in group.keys if key not in context]
                        if kind == "reference" and len(missing) == len(group.keys):
                            continue
                        if missing:
                            raise ValueError(f"Output group {field!r} is missing keys {missing}")
                        identity = tuple((key, _key(context[key])) for key in group.keys)
                    buckets.setdefault(identity, []).append((kind, position, context))
            if not buckets and group.kind == "once":
                buckets[("once",)] = [("batch", None, self._batch)]
            elif not buckets and group.kind == "by" and all(key in self._batch for key in group.keys):
                identity = tuple((key, _key(self._batch[key])) for key in group.keys)
                buckets[identity] = [("batch", None, self._batch)]
            planned = []
            for group_index, members in enumerate(buckets.values()):
                owners += 1
                paths = []
                examples = []
                key_values = tuple((key, members[0][2][key]) for key in group.keys)
                for kind, position, context in members:
                    values = dict(context, group_index=group_index)
                    relative, parts = _expand(pattern, values)
                    paths.append(relative)
                    example = {"field": field, "row_position": position if kind == "row" else None,
                               "row_index": self._row_indexes[position] if kind == "row" and position is not None else None,
                               "group_index": group_index,
                               "group_key": {key: str(value) if isinstance(value, Path) else value for key, value in key_values},
                               "parts": parts}
                    examples.append(example)
                    if position is not None:
                        target = row_paths if kind == "row" else reference_paths
                        target[position][field] = str(self._destination(relative))
                if len({self._comparison(path) for path in paths}) > 1:
                    mismatches.append({"path": paths[0], "members": examples[:_MAX_EXAMPLES], "total_members": len(examples)})
                item = PlannedFile(self._destination(paths[0]), group_index, key_values,
                                   tuple(position for kind, position, _ in members if kind == "row" and position is not None),
                                   tuple(position for kind, position, _ in members if kind == "reference" and position is not None))
                # Equivalent filesystem spellings identify one destination;
                # every contributor receives the chosen exact owner path.
                for kind, position, _ in members:
                    if position is not None:
                        target = row_paths if kind == "row" else reference_paths
                        target[position][field] = str(item.path)
                planned.append(item)
                destinations.setdefault(self._comparison(paths[0]), []).append({"path": paths[0], "example": examples[0]})
                if group.kind == "once":
                    batch_paths[field] = str(item.path)
            output_files[field] = tuple(planned)
        for items in destinations.values():
            if len(items) > 1:
                conflicts.append({"path": items[0]["path"], "members": [item["example"] for item in items[:_MAX_EXAMPLES]], "total_members": len(items)})
        if mismatches or conflicts:
            is_mismatch = bool(mismatches)
            issues = mismatches if is_mismatch else conflicts
            code = "output_group_destination_mismatch" if is_mismatch else "output_path_collision"
            detail = ("Rows contributing to one file request different destinations." if is_mismatch else "Different output files request the same destination.")
            report = {"code": code, "node_id": self.node_id, "detail": detail,
                      "fields": [item for item in fields.values() if item["kind"] == "exact"], "conflicts": issues[:_MAX_EXAMPLES],
                      "total_conflicts": len(issues), "total_owners": owners, "suggested_templates": {}}
            if suggestions:
                changed = {member["field"] for issue in issues for member in issue["members"]}
                for fallback in (False, True):
                    candidate = {}
                    for field in changed:
                        declaration = fields[field]
                        token = "{row_index}" if declaration["group"]["kind"] == "per_row" and not fallback else "{group_index}"
                        pattern = declaration["template"]
                        candidate[field] = pattern.replace("{row_index}", "{group_index}") if is_mismatch else _suffix(pattern, token)
                    trial = deepcopy(fields)
                    for field, pattern in candidate.items():
                        trial[field]["template"] = pattern
                    try:
                        self._plan(trial, suggestions=False)
                    except (ValueError, TypeError, CacheCorruptionError):
                        continue
                    report["suggested_templates"] = candidate
                    break
            evidence = self if self._bounded() else None
            error = OutputGroupDestinationMismatchError if is_mismatch else OutputPathCollisionError
            raise error(report, evidence)
        return OutputFilePlan(output_files, row_paths, reference_paths, batch_paths)

    def _destination(self, relative: str) -> Path:
        path = self.assets_dir / relative
        if not path.resolve().is_relative_to(self.assets_dir.resolve()):
            raise CacheCorruptionError(f"Output template escapes invocation assets: {relative!r}")
        return path

    def _comparison(self, path: str) -> str:
        path = unicodedata.normalize("NFC", path)
        return path if self.case_sensitive else path.casefold()

    def _bounded(self) -> bool:
        return len(json.dumps([self._rows, self._references, self._batch], default=str)) <= _MAX_EVIDENCE_BYTES


def _suffix(pattern: str, token: str) -> str:
    extension = re.search(r"(\{(?:ext|\w+\.(?:ext|exts))\})$", pattern)
    if extension:
        prefix = pattern[:extension.start()]
        marker = prefix[-4:] if prefix.lower().endswith(".ome") else ""
        if marker:
            prefix = prefix[:-len(marker)]
        return prefix + "_" + token + marker + extension.group()
    for extension in (".ome.tiff", ".ome.tif", ".nii.gz", ".tar.gz"):
        if pattern.lower().endswith(extension):
            return pattern[:-len(extension)] + "_" + token + pattern[-len(extension):]
    if pattern.endswith("}"):
        # An arbitrary value may contain its own extension; do not move it blindly.
        return pattern
    suffix = Path(pattern).suffix
    return (pattern[:-len(suffix)] + "_" + token + suffix) if suffix else pattern + "_" + token


def _filesystem_case_sensitive(path: Path) -> bool:
    # Inspect the existing invocation root without creating probe files.
    path = path.resolve()
    for part in (path, *path.parents):
        swapped = part.with_name(part.name.swapcase()) if part.name else part
        if swapped != part and swapped.exists():
            try:
                if swapped.samefile(part):
                    return False
            except OSError:
                pass
    return os.name != "nt"


def inspect_output_files(evidence: OutputPlanningEvidence | None, templates: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Return proven current-input warnings; absent evidence means unknown, silently."""
    if evidence is None:
        return []
    try:
        evidence.check_templates(templates)
    except OutputPlanningError as error:
        return [error.report]
    return []


def inspect_constant_output_files(tool: Any, arguments: Mapping[str, Any], *, node_id: str,
                                  templates: Mapping[str, str] | None = None,
                                  assets_dir: Path | None = None) -> list[dict[str, Any]]:
    """Inspect an actual constant-only invocation without callbacks or runtime setup.

    The caller must omit invocations with unknown column/interface bindings.
    Missing required inputs and timestamp-dependent names remain silent.
    """
    from bioimageflow.validation import is_path_type
    from bioimageflow.engine.common import _absolute_runtime_path
    declarations = output_file_declarations(tool, templates)
    if any("{timestamp}" in item["template"] for item in declarations.values()):
        return []
    annotations = tool.Inputs._get_all_annotations()
    values = dict(arguments)
    for field, annotation in annotations.items():
        if field not in values:
            if not hasattr(tool.Inputs, field):
                return []
            values[field] = getattr(tool.Inputs, field)
        if is_path_type(annotation):
            values[field] = _absolute_runtime_path(values[field])
            if values[field] is not None:
                values[field] = Path(values[field])
    paths = [values[field] for field, annotation in annotations.items()
             if is_path_type(annotation) and values.get(field) is not None]
    context = dict(values, node_name=node_id, row_index="0",
                   _ext=Path(str(paths[0])).suffix if len(paths) == 1 else "", _columns={})
    collective = tool.row_consumption.value == "collective"
    try:
        evidence = OutputPlanningEvidence.capture(node_id=node_id,
            assets_dir=assets_dir or Path.cwd() / ".output-inspection" / "assets",
            tool=tool, row_contexts=[] if collective else [context],
            row_indexes=[] if collective else ["0"],
            batch_context=dict(context, row_index="batch"), templates=templates)
        return inspect_output_files(evidence)
    except (ValueError, TypeError, CacheCorruptionError):
        return []


def inspect_workflow_output_files(workflow: Any) -> list[dict[str, Any]]:
    """Inspect provable current constant/default destinations without executing nodes."""
    from bioimageflow_core import ProcessingTool
    from bioimageflow.workflow_node import WorkflowNode
    reports = []

    def inspect(definition: Any, prefix: str) -> None:
        for name, node in definition.nodes.items():
            if not node.enabled:
                continue
            scoped = f"{prefix}/{name}" if prefix else name
            if isinstance(node, WorkflowNode):
                if not (node._workflow_input_bindings or node._input_column_bindings
                        or node._input_dataframe_bindings or node._input_constant_bindings):
                    inspect(node.workflow, scoped)
                continue
            if not isinstance(node.tool, ProcessingTool) or node._column_bindings or node._workflow_input_bindings:
                continue
            reports.extend(inspect_constant_output_files(node.tool, node._constant_bindings,
                node_id=scoped, templates=node.output_templates,
                assets_dir=Path(definition.storage_path) / ".output-inspection" / "assets"))
    inspect(workflow, "")
    return reports
