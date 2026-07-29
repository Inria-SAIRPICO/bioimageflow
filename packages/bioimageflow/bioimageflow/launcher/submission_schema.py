"""Nested wire validation for launcher submission payloads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from bioimageflow.parsl.types import ExecutorBinding, ParslTaskPolicy
from bioimageflow.storage import canonical_json_bytes

from .inputs import INVOCATION_SCHEMA, decode_typed_constant
from .schemas import (
    SHA256_PATTERN,
    SUBMISSION_FIELDS,
    SUBMISSION_SCHEMA,
    LauncherSchemaError,
    _exact_object,
    _mapping,
    _require_integer,
    _require_mapping,
    _require_string,
    _require_string_mapping,
    _validate_absolute_path,
    _validate_relative_posix_path,
    parse_utc_timestamp,
    validate_run_id,
)
from .types import ParslConfigRef, launch_config_from_dict


WORKFLOW_FIELDS = frozenset({"kind", "digest", "payload"})
PARSL_CONFIG_FIELDS = frozenset({"factory", "kwargs", "secret_refs"})
EXECUTOR_BINDING_FIELDS = frozenset({"schema", "label", "environments", "capabilities"})
TASK_POLICY_FIELDS = frozenset({"schema", "row_chunk_size", "max_in_flight"})
LAUNCH_FIELDS = frozenset({"backend", "work_dir", "hard_cancel_after"})
PSIJ_LAUNCH_FIELDS = frozenset(
    {
        "backend",
        "executor",
        "walltime_seconds",
        "queue",
        "project",
        "cpu_cores",
        "work_dir",
        "hard_cancel_after",
    }
)
PROTOCOL_VERSION_FIELDS = frozenset(
    {
        "launcher",
        "workflow_graph",
        "workflow_archive",
        "parsl_task",
        "parsl_result",
    }
)
ROOT_INVOCATION_FIELDS = frozenset({"schema", "variant", "inputs", "outputs"})
TARGET_INVOCATION_FIELDS = frozenset({"schema", "variant", "targets"})
FIELD_INPUT_FIELDS = frozenset({"id", "kind", "name", "value"})
DATAFRAME_INPUT_FIELDS = frozenset({"dataframe", "id", "kind", "name"})
DATAFRAME_FIELDS = frozenset(
    {
        "index",
        "logical_digest",
        "logical_schema",
        "path",
        "path_cells",
        "transport_digest",
    }
)
INDEX_FIELDS = frozenset({"dtypes", "kind", "length", "names"})
PATH_CELL_FIELDS = frozenset({"column", "row_position"})
OUTPUT_FIELDS = frozenset({"id", "name"})


def _validate_workflow(value: object) -> str:
    workflow = _exact_object(
        value,
        field="workflow",
        fields=WORKFLOW_FIELDS,
    )
    kind = workflow["kind"]
    if kind not in {"graph_v1", "archive_v1"}:
        raise LauncherSchemaError("workflow.kind must be 'graph_v1' or 'archive_v1'.")
    digest = workflow["digest"]
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise LauncherSchemaError(
            "workflow.digest must use 'sha256:' and a lowercase SHA-256 digest."
        )
    payload = workflow["payload"]
    _require_mapping(payload, field="workflow.payload")
    assert isinstance(payload, Mapping)
    version_field = "schema_version" if kind == "graph_v1" else "archive_version"
    if payload.get(version_field) != 1 or type(payload.get(version_field)) is not int:
        raise LauncherSchemaError(
            f"workflow.payload.{version_field} must be integer 1."
        )
    expected = f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"
    if digest != expected:
        raise LauncherSchemaError("workflow.digest does not match workflow.payload.")
    return kind


def _validate_dataframe_input(value: object, *, field: str) -> None:
    dataframe = _exact_object(value, field=field, fields=DATAFRAME_FIELDS)
    path = _validate_relative_posix_path(dataframe["path"], field=f"{field}.path")
    if not path.startswith("inputs/") or not path.endswith(".parquet"):
        raise LauncherSchemaError(f"{field}.path must name one inputs/ Parquet file.")
    for digest_field in ("logical_digest", "transport_digest"):
        digest = dataframe[digest_field]
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise LauncherSchemaError(
                f"{field}.{digest_field} must be a lowercase SHA-256 digest."
            )
    if not isinstance(dataframe["logical_schema"], list):
        raise LauncherSchemaError(f"{field}.logical_schema must be an array.")
    index = _exact_object(
        dataframe["index"],
        field=f"{field}.index",
        fields=INDEX_FIELDS,
    )
    if index["kind"] not in {"index", "multi_index"}:
        raise LauncherSchemaError(f"{field}.index.kind is invalid.")
    _require_integer(index["length"], field=f"{field}.index.length")
    names = index["names"]
    dtypes = index["dtypes"]
    if (
        not isinstance(names, list)
        or not names
        or any(name is not None and type(name) is not str for name in names)
        or not isinstance(dtypes, list)
        or len(dtypes) != len(names)
        or any(type(dtype) is not str or not dtype for dtype in dtypes)
    ):
        raise LauncherSchemaError(f"{field}.index names or dtypes are invalid.")
    expected_levels = 1 if index["kind"] == "index" else len(names)
    if len(names) != expected_levels:
        raise LauncherSchemaError(f"{field}.index level count is invalid.")
    path_cells = dataframe["path_cells"]
    if not isinstance(path_cells, list):
        raise LauncherSchemaError(f"{field}.path_cells must be an array.")
    seen_cells: set[tuple[str, int]] = set()
    for position, raw_cell in enumerate(path_cells):
        cell_field = f"{field}.path_cells[{position}]"
        cell = _exact_object(
            raw_cell,
            field=cell_field,
            fields=PATH_CELL_FIELDS,
        )
        _require_string(cell["column"], field=f"{cell_field}.column")
        _require_integer(
            cell["row_position"],
            field=f"{cell_field}.row_position",
        )
        identity = (cell["column"], cell["row_position"])
        if identity in seen_cells:
            raise LauncherSchemaError(f"{field}.path_cells must be unique.")
        seen_cells.add(identity)


def _validate_invocation(value: object) -> None:
    _require_mapping(value, field="invocation")
    assert isinstance(value, Mapping)
    variant = value.get("variant")
    fields = (
        ROOT_INVOCATION_FIELDS
        if variant == "root"
        else TARGET_INVOCATION_FIELDS
        if variant == "targets"
        else frozenset()
    )
    if not fields:
        raise LauncherSchemaError("invocation.variant must be 'root' or 'targets'.")
    invocation = _exact_object(value, field="invocation", fields=fields)
    if invocation["schema"] != INVOCATION_SCHEMA:
        raise LauncherSchemaError(f"invocation.schema must be {INVOCATION_SCHEMA!r}.")
    if variant == "targets":
        targets = invocation["targets"]
        if (
            not isinstance(targets, list)
            or not targets
            or any(type(target) is not str or not target for target in targets)
            or len(set(targets)) != len(targets)
        ):
            raise LauncherSchemaError(
                "invocation.targets must be a non-empty array of unique strings."
            )
        return

    raw_inputs = invocation["inputs"]
    raw_outputs = invocation["outputs"]
    if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
        raise LauncherSchemaError("invocation inputs and outputs must be arrays.")
    seen_inputs: set[str] = set()
    for position, raw_input in enumerate(raw_inputs):
        field = f"invocation.inputs[{position}]"
        _require_mapping(raw_input, field=field)
        assert isinstance(raw_input, Mapping)
        kind = raw_input.get("kind")
        expected = (
            FIELD_INPUT_FIELDS
            if kind == "field"
            else DATAFRAME_INPUT_FIELDS
            if kind == "dataframe"
            else frozenset()
        )
        if not expected:
            raise LauncherSchemaError(f"{field}.kind is invalid.")
        item = _exact_object(raw_input, field=field, fields=expected)
        for name in ("id", "name"):
            _require_string(item[name], field=f"{field}.{name}")
        if item["id"] in seen_inputs:
            raise LauncherSchemaError("invocation input IDs must be unique.")
        seen_inputs.add(item["id"])
        if kind == "field":
            try:
                decode_typed_constant(item["value"])
            except (TypeError, ValueError) as error:
                raise LauncherSchemaError(
                    f"{field}.value is not a valid typed constant."
                ) from error
        else:
            _validate_dataframe_input(
                item["dataframe"],
                field=f"{field}.dataframe",
            )
    seen_outputs: set[str] = set()
    for position, raw_output in enumerate(raw_outputs):
        field = f"invocation.outputs[{position}]"
        output = _exact_object(raw_output, field=field, fields=OUTPUT_FIELDS)
        for name in ("id", "name"):
            _require_string(output[name], field=f"{field}.{name}")
        if output["id"] in seen_outputs:
            raise LauncherSchemaError("invocation output IDs must be unique.")
        seen_outputs.add(output["id"])


def _validate_submission_records(result: Mapping[str, Any]) -> None:
    parsl_config = _exact_object(
        result["parsl_config"],
        field="parsl_config",
        fields=PARSL_CONFIG_FIELDS,
    )
    try:
        ParslConfigRef.from_dict(parsl_config)
    except (TypeError, ValueError) as error:
        raise LauncherSchemaError("parsl_config is invalid.") from error

    bindings = result["executor_bindings"]
    _require_mapping(bindings, field="executor_bindings")
    assert isinstance(bindings, Mapping)
    if not bindings:
        raise LauncherSchemaError("executor_bindings must not be empty.")
    for label, raw_binding in bindings.items():
        _require_string(label, field="executor_bindings label")
        binding = _exact_object(
            raw_binding,
            field=f"executor_bindings.{label}",
            fields=EXECUTOR_BINDING_FIELDS,
        )
        try:
            parsed = ExecutorBinding.from_dict(binding)
        except (TypeError, ValueError) as error:
            raise LauncherSchemaError(
                f"executor_bindings.{label} is invalid."
            ) from error
        if parsed.label != label:
            raise LauncherSchemaError(
                f"executor_bindings.{label}.label must match its mapping key."
            )

    labels = frozenset(bindings)
    for route_field in ("node_routes", "environment_routes"):
        routes = result[route_field]
        _require_string_mapping(routes, field=route_field, nullable=True)
        if routes is not None and any(label not in labels for label in routes.values()):
            raise LauncherSchemaError(
                f"{route_field} contains an unknown executor label."
            )

    task_policy = _exact_object(
        result["task_policy"],
        field="task_policy",
        fields=TASK_POLICY_FIELDS,
    )
    try:
        ParslTaskPolicy.from_dict(task_policy)
    except (TypeError, ValueError) as error:
        raise LauncherSchemaError("task_policy is invalid.") from error

    raw_launch = result["launch"]
    _require_mapping(raw_launch, field="launch")
    assert isinstance(raw_launch, Mapping)
    launch_fields = (
        PSIJ_LAUNCH_FIELDS
        if raw_launch.get("backend") == "psij"
        else LAUNCH_FIELDS
    )
    launch = _exact_object(raw_launch, field="launch", fields=launch_fields)
    try:
        launch_config_from_dict(launch)
    except (TypeError, ValueError) as error:
        raise LauncherSchemaError("launch is invalid.") from error

    versions = _exact_object(
        result["protocol_versions"],
        field="protocol_versions",
        fields=PROTOCOL_VERSION_FIELDS,
    )
    if any(type(version) is not int or version != 1 for version in versions.values()):
        raise LauncherSchemaError("protocol_versions values must all be integer 1.")


def validate_submission_payload(payload: object) -> dict[str, Any]:
    """Validate and copy a submission-v1 payload and all nested records."""
    result = _mapping(
        payload,
        schema=SUBMISSION_SCHEMA,
        fields=SUBMISSION_FIELDS,
    )
    run_id = validate_run_id(result["run_id"])
    parse_utc_timestamp(result["created_at"], field="created_at")
    _validate_absolute_path(result["storage_root"], field="storage_root")
    canonical_view = _validate_relative_posix_path(
        result["canonical_view"],
        field="canonical_view",
    )
    if canonical_view != f"views/runs/{run_id}":
        raise LauncherSchemaError(
            "canonical_view must be the run's exact canonical view path."
        )
    workflow_kind = _validate_workflow(result["workflow"])
    _validate_invocation(result["invocation"])
    _validate_absolute_path(
        result["shared_runtime_root"],
        field="shared_runtime_root",
        nullable=True,
    )
    if workflow_kind == "archive_v1" and result["shared_runtime_root"] is None:
        raise LauncherSchemaError("archive_v1 workflows require shared_runtime_root.")
    _validate_submission_records(result)
    return result
