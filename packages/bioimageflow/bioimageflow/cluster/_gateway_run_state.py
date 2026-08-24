"""Managed-run lifecycle operations for the cluster gateway."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._common import DIGEST_RE, canonical_digest
from ._gateway_archive import (
    _canonical_attempt_id,
    _canonical_run_id,
    _extract_invocation_archive,
    _invocation_manifest,
    _supported_scheduler_job,
    _validate_invocation_archive,
)
from ._gateway_support import (
    _MAX_UPLOAD_BYTES,
    _RUN_PHASES,
    _RUN_RECORD_SCHEMA,
    GatewayOperationFailure,
    _atomic_private_bytes,
    _atomic_private_json,
    _attest_existing_python,
    _child_environment,
    _current_gateway_binding,
    _exact_arguments,
    _failure,
    _run_json_child,
    _stat_private_directory,
    _validate_deployment_archive,
    _validate_published_file,
    _verify_extracted_deployment,
)
from ._gateway_wire import parse_execution_plan, parse_retry_plan
from ._gateway_uv import attest_managed_uv
from .protocol import MAX_REQUEST_BYTES, GatewayRequest


class GatewayRunMixin:
    def _run_directory(self, run_id: Any) -> Path:
        return self.root / "runs" / _canonical_run_id(run_id)

    def _read_run_record(self, run_id: Any) -> dict[str, Any]:
        canonical = _canonical_run_id(run_id)
        path = self._run_directory(canonical) / "record.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise _failure(
                "run-not-found", "The requested managed run does not exist.",
                phase="run-observation", retry_safety="not-applicable",
                next_action="check-run-id", identities={"run_id": canonical},
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_nlink != 1
                or metadata.st_size > MAX_REQUEST_BYTES
            ):
                raise _failure("operation-record-tampered", "The run index is unsafe.")
            encoded = b""
            while chunk := os.read(descriptor, 64 * 1024):
                encoded += chunk
        finally:
            os.close(descriptor)
        try:
            value = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure("operation-record-tampered", "The run index is malformed.") from exc
        fields = {
            "schema", "run_id", "attempt_id", "request_digest", "plan_digest",
            "deployment_id", "invocation_digest", "validation_digest", "object_id",
            "object_size", "storage_path", "phase", "launcher_bound", "plan",
            "invocation_manifest", "revision", "updated_at",
            "gateway_publication_id", "gateway_artifact_digest",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value["schema"] != _RUN_RECORD_SCHEMA
            or value["run_id"] != canonical
            or value["phase"] not in _RUN_PHASES
            or type(value["launcher_bound"]) is not bool
            or type(value["gateway_publication_id"]) is not str
            or not value["gateway_publication_id"]
            or type(value["gateway_artifact_digest"]) is not str
            or DIGEST_RE.fullmatch(value["gateway_artifact_digest"]) is None
            or type(value["plan"]) is not dict
            or type(value["invocation_manifest"]) is not dict
            or type(value["revision"]) is not int
            or value["revision"] < 0
            or any(
                type(value[name]) is not str or DIGEST_RE.fullmatch(value[name]) is None
                for name in (
                    "request_digest", "plan_digest", "deployment_id",
                    "invocation_digest", "validation_digest", "object_id",
                )
            )
            or type(value["object_size"]) is not int
            or not 0 <= value["object_size"] <= _MAX_UPLOAD_BYTES
        ):
            raise _failure("operation-record-tampered", "The run index is malformed.")
        _canonical_attempt_id(value["attempt_id"])
        if (
            canonical_digest(
                {
                    key: item
                    for key, item in value["plan"].items()
                    if key != "plan_digest"
                }
            )
            != value["plan_digest"]
        ):
            raise _failure(
                "operation-record-tampered", "The retained plan digest is invalid."
            )
        current_publication, current_artifact = _current_gateway_binding()
        if (
            value["gateway_publication_id"] != current_publication
            or value["gateway_artifact_digest"] != current_artifact
        ):
            raise _failure(
                "gateway-untrusted",
                "The retained run belongs to another immutable gateway publication.",
                phase="run-observation",
                retry_safety="not-applicable",
                next_action="attach-through-recorded-gateway-publication",
                identities={"run_id": canonical},
            )
        return value

    def _write_run_record(self, record: Mapping[str, Any]) -> None:
        run_id = _canonical_run_id(record.get("run_id"))
        path = self._run_directory(run_id) / "record.json"
        _atomic_private_json(path, record)
        if self._read_run_record(run_id) != dict(record):
            raise _failure("operation-record-tampered", "The run index was not durable.")

    @staticmethod
    def _allocated_observation(record: Mapping[str, Any]) -> dict[str, Any]:
        state = "cancelled" if record["phase"] == "cancelled" else "prepared"
        return {
            "schema": "bioimageflow.launcher.run-observation.v1",
            "error": None,
            "retry_plan": None,
            "run_id": record["run_id"],
            "state": state,
            "status_revision": record["revision"],
            "storage_path": record["storage_path"],
            "terminal": state == "cancelled",
            "updated_at": record["updated_at"],
            "attempt_phase": record["phase"],
            "gateway_publication_id": record["gateway_publication_id"],
            "gateway_artifact_digest": record["gateway_artifact_digest"],
        }

    @staticmethod
    def _bound_observation(
        record: Mapping[str, Any], observation: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            **dict(observation),
            "attempt_phase": record["phase"],
            "gateway_publication_id": record["gateway_publication_id"],
            "gateway_artifact_digest": record["gateway_artifact_digest"],
        }

    def _deployment_runtime(
        self, record: Mapping[str, Any], *, require_validation: bool = False
    ) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        deployment_id = record["deployment_id"]
        destination = self.root / "deployments" / deployment_id[7:]
        _stat_private_directory(destination)
        try:
            publication = json.loads((destination / "publication.json").read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure("deployment-tampered", "The deployment publication is malformed.") from exc
        if (
            publication.get("deployment_id") != deployment_id
            or publication.get("environment_installed") is not True
        ):
            raise _failure("deployment-tampered", "The retained deployment is unavailable.")
        artifact = destination / "artifact.zip"
        _validate_published_file(artifact, publication["object_id"])
        manifest = _validate_deployment_archive(
            artifact,
            publication["prepared_deployment_id"],
            publication["manifest_digest"],
        )
        _verify_extracted_deployment(destination / "content", manifest)
        for name, field in (
            ("activation.sh", "activation_digest"),
            ("submit_run.py", "run_submitter_digest"),
            ("control_run.py", "run_controller_digest"),
        ):
            _validate_published_file(destination / name, publication[field])
        if publication.get("environment_kind") == "uv":
            attestation, digest, runtime_python = attest_managed_uv(
                destination,
                manifest,
                publication,
                failure_category="environment-platform-incompatible",
            )
            attestation = {**attestation, "requested_executable": runtime_python}
            expected_attestation_digest = publication.get(
                "environment_attestation_digest"
            )
        else:
            attestation, digest = _attest_existing_python(
                manifest, failure_category="external-environment-changed"
            )
            expected_attestation_digest = publication.get(
                "external_attestation_digest"
            )
        if digest != expected_attestation_digest:
            changed_category = (
                "environment-platform-incompatible"
                if publication.get("environment_kind") == "uv"
                else "external-environment-changed"
            )
            raise _failure(
                changed_category,
                "The selected Python environment changed after deployment confirmation.",
                phase="submission", retry_safety="safe",
                next_action="deploy-and-confirm-again",
                identities={"deployment_id": deployment_id},
            )
        validation: dict[str, Any] | None = None
        if require_validation:
            validation_path = destination / "validations" / f"{record['validation_digest'][7:]}.json"
            try:
                loaded_validation = json.loads(validation_path.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _failure("validation-expired", "The retained validation is unavailable.") from exc
            if type(loaded_validation) is not dict:
                raise _failure("deployment-tampered", "The retained validation changed.")
            validation = loaded_validation
            if (
                validation.get("validation_digest") != record["validation_digest"]
                or validation.get("deployment_id") != deployment_id
                or validation.get("valid") is not True
                or canonical_digest({key: item for key, item in validation.items() if key != "validation_digest"}) != record["validation_digest"]
            ):
                raise _failure("deployment-tampered", "The retained validation changed.")
            try:
                expires = datetime.fromisoformat(validation["expires_at"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise _failure("deployment-tampered", "The retained validation expiry is invalid.") from exc
            if datetime.now(timezone.utc) >= expires:
                raise _failure(
                    "validation-expired", "The retained validation report expired.",
                    phase="submission", retry_safety="safe",
                    next_action="validate-and-plan-again",
                )
        return destination, manifest, attestation, validation

    def _run_controller(
        self,
        record: Mapping[str, Any],
        operation: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        deployment, _manifest, attestation, _validation = self._deployment_runtime(record)
        requests = self._private_subdirectory(self.root / "temporary", "run-requests")
        private = requests / uuid.uuid4().hex
        private.mkdir(mode=0o700)
        try:
            request = {
                "operation": operation,
                "arguments": dict(arguments),
                "storage_path": record["storage_path"],
                "run_id": record["run_id"],
                "transfer_root": str(self._private_subdirectory(self.root / "transfers", "runtime")),
                "request_id": str(uuid.uuid4()),
                "request_digest": canonical_digest({"operation": operation, "arguments": dict(arguments)}),
            }
            path = private / "request.json"
            _atomic_private_json(path, request)
            response = _run_json_child(
                [attestation["requested_executable"], "-I", "-B", str(deployment / "control_run.py"), str(path)],
                environment=_child_environment(),
                timeout=60.0,
                output_limit=MAX_REQUEST_BYTES,
                failure_category="remote-operation-failed",
            )
        finally:
            shutil.rmtree(private, ignore_errors=True)
        if response.get("status") == "error":
            raise _failure(
                str(response.get("category", "remote-operation-failed")),
                str(response.get("message", "The managed run operation failed.")),
                phase=operation, retry_safety="safe", next_action="inspect-run",
                identities={"run_id": record["run_id"]},
            )
        if set(response) != {"status", "payload"} or response["status"] != "ok" or type(response["payload"]) is not dict:
            raise _failure("remote-operation-failed", "The managed run response is malformed.")
        return response["payload"]

    def submit_plan_request(self, request: GatewayRequest) -> dict[str, Any]:
        if request.operation_id is None or request.payload_digest is None:
            raise _failure("protocol-incompatible", "submit-plan requires stable attempt and payload identities.")
        value = _exact_arguments(
            request.arguments, {"plan", "invocation_manifest", "object_id", "object_size"}
        )
        try:
            plan = parse_execution_plan(value["plan"])
        except (TypeError, ValueError) as exc:
            raise _failure("protocol-incompatible", "The remote execution plan is invalid.") from exc
        _supported_scheduler_job(
            plan.scheduler_job.to_dict(),
            expected_scheduler=plan.scheduler_job.scheduler,
        )
        if (
            plan.attempt_id != request.operation_id
            or plan.cluster_root != str(self.root)
            or value["object_id"] != request.payload_digest
            or type(value["object_size"]) is not int
            or not 0 <= value["object_size"] <= _MAX_UPLOAD_BYTES
            or type(value["object_id"]) is not str
            or DIGEST_RE.fullmatch(value["object_id"]) is None
        ):
            raise _failure("operation-conflict", "The submit attempt bindings do not match.")
        invocation = _invocation_manifest(
            value["invocation_manifest"], expected_digest=plan.invocation_digest
        )
        run_dir = self._run_directory(plan.run_id)
        if not run_dir.exists():
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
        _stat_private_directory(run_dir)
        record_path = run_dir / "record.json"
        if record_path.exists():
            record = self._read_run_record(plan.run_id)
            if record["request_digest"] != request.operation_digest:
                raise _failure("operation-conflict", "The run ID is bound to another plan.")
        else:
            gateway_publication_id, gateway_artifact_digest = _current_gateway_binding()
            record = {
                "schema": _RUN_RECORD_SCHEMA,
                "run_id": plan.run_id,
                "attempt_id": plan.attempt_id,
                "request_digest": request.operation_digest,
                "plan_digest": plan.plan_digest,
                "deployment_id": plan.deployment_id,
                "invocation_digest": plan.invocation_digest,
                "validation_digest": plan.validation_digest,
                "object_id": value["object_id"],
                "object_size": value["object_size"],
                "storage_path": plan.storage_path,
                "phase": "allocated",
                "launcher_bound": False,
                "plan": plan.to_dict(),
                "invocation_manifest": invocation,
                "gateway_publication_id": gateway_publication_id,
                "gateway_artifact_digest": gateway_artifact_digest,
                "revision": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
            self._write_run_record(record)
        if record["phase"] in {"submitted", "cancelled"}:
            observation = (
                self._bound_observation(
                    record, self._run_controller(record, "inspect-run", {})
                )
                if record["launcher_bound"]
                else self._allocated_observation(record)
            )
            return {"run_id": plan.run_id, "observation": observation, "upload_required": False}
        if record["phase"] in {"scheduler-intent", "uncertain"}:
            raise _failure(
                "submission-uncertain", "Scheduler acceptance cannot be disproved; the attempt was not resubmitted.",
                phase="submission", allocation_state="unknown", retry_safety="same-attempt-only",
                next_action="attach-run", identities={"run_id": plan.run_id, "attempt_id": plan.attempt_id},
            )
        object_path = self.root / "objects" / f"{record['object_id'][7:]}.object"
        if not object_path.exists():
            if record["phase"] != "uploading":
                record = {**record, "phase": "uploading", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                self._write_run_record(record)
            return {"run_id": plan.run_id, "observation": self._allocated_observation(record), "upload_required": True}
        _validate_invocation_archive(object_path, invocation, record["object_id"])
        invocation_root = run_dir / "invocation"
        if not invocation_root.exists():
            _extract_invocation_archive(object_path, invocation_root, invocation)
        if record["phase"] != "ready":
            record = {**record, "phase": "ready", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
            self._write_run_record(record)
        deployment, manifest, attestation, validation = self._deployment_runtime(record, require_validation=True)
        assert validation is not None
        plan_value = plan.to_dict()
        if (
            plan.validation_expires_at != validation["expires_at"]
            or plan_value["validation_evidence"] != validation["evidence"]
            or plan_value["executor_claims"] != validation["executor_bindings"]
        ):
            raise _failure(
                "parsl-configuration-changed",
                "The retained validation claims no longer match the execution plan.",
                phase="submission", retry_safety="safe",
                next_action="validate-and-plan-again",
            )
        scheduler = plan.scheduler_job
        if scheduler.gpu or scheduler.memory is not None or scheduler.attributes:
            raise _failure(
                "unsupported-scheduler-adapter", "The current PSI/J bridge cannot represent GPU, memory, or custom scheduler attributes.",
                phase="submission", retry_safety="safe", next_action="simplify-orchestrator-job",
            )
        parsl = manifest["parsl"]
        factory = (
            f"factory:{parsl['factory']}"
            if parsl["source_kind"] == "file"
            else parsl["source"]
        )
        handoff = run_dir / "secret-handoff.sh"
        secret_lines = ["set -eu"]
        total = 0
        for reference in parsl["secret_refs"].values():
            if reference not in os.environ:
                raise _failure(
                    "secret-reference-missing", f"Required secret reference {reference!r} is unavailable.",
                    phase="submission", retry_safety="safe", next_action="provide-secret-reference",
                    identities={"run_id": plan.run_id},
                )
            secret = os.environ[reference]
            size = len(secret.encode("utf-8"))
            total += size
            if "\0" in secret or size > 64 * 1024 or total > 256 * 1024:
                raise _failure("resource-limit-exceeded", "Resolved submission secrets exceed their handoff limit.")
            secret_lines.append(f"export {reference}={shlex.quote(secret)}")
        if len(secret_lines) > 1 and not handoff.exists():
            _atomic_private_bytes(handoff, ("\n".join(secret_lines) + "\n").encode(), 0o600)
        python_paths = [str(deployment / "content" / "parsl")]
        include = deployment / "content" / "parsl" / "include"
        if include.is_dir():
            python_paths.extend(str(child) for child in sorted(include.iterdir()) if child.is_dir())
        pre_launch = [
            "set -eu",
            f". {shlex.quote(str(deployment / 'activation.sh'))}",
            f"export PYTHONPATH={shlex.quote(':'.join(python_paths))}${{PYTHONPATH:+:$PYTHONPATH}}",
        ]
        setup = deployment / "content" / "setup" / "setup.sh"
        if setup.is_file():
            pre_launch.append(f". {shlex.quote(str(setup))}")
        if len(secret_lines) > 1:
            pre_launch.extend([f". {shlex.quote(str(handoff))}", f"rm -f -- {shlex.quote(str(handoff))}"])
        node_routes = {
            item["node"]: item["selected_executor"]
            for item in plan_value["nodes"]
            if item["kind"] == "processing" and item["will_dispatch"]
        }
        runtime_root = run_dir / "runtime"
        runtime_root.mkdir(mode=0o700, exist_ok=True)
        child_request = {
            "run_id": plan.run_id,
            "storage_path": plan.storage_path,
            "invocation_root": str(invocation_root),
            "deployment_content": str(deployment / "content"),
            "shared_runtime_root": str(runtime_root),
            "parsl_config": {"factory": factory, "kwargs": parsl["kwargs"], "secret_refs": parsl["secret_refs"]},
            "executor_bindings": validation["executor_bindings"],
            "node_routes": node_routes,
            "launch": {
                "backend": "psij", "executor": scheduler.scheduler,
                "walltime_seconds": scheduler.walltime_seconds,
                "queue": scheduler.queue, "project": scheduler.project,
                "cpu_cores": scheduler.cpu, "work_dir": str(run_dir),
                "hard_cancel_after": scheduler.hard_cancel_after,
            },
            "pre_launch": "\n".join(pre_launch) + "\n",
        }
        record = {**record, "phase": "scheduler-intent", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        self._write_run_record(record)
        requests = self._private_subdirectory(self.root / "temporary", "run-requests")
        private = requests / uuid.uuid4().hex
        private.mkdir(mode=0o700)
        try:
            child_path = private / "request.json"
            _atomic_private_json(child_path, child_request)
            try:
                response = _run_json_child(
                    [attestation["requested_executable"], "-I", "-B", str(deployment / "submit_run.py"), str(child_path)],
                    environment=_child_environment(), timeout=120.0,
                    output_limit=MAX_REQUEST_BYTES,
                    failure_category="remote-operation-failed",
                )
            except GatewayOperationFailure:
                record = {**record, "phase": "uncertain", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                self._write_run_record(record)
                raise _failure(
                    "submission-uncertain", "Scheduler submission may have occurred; the attempt was not resubmitted.",
                    phase="submission", allocation_state="unknown", retry_safety="same-attempt-only",
                    next_action="attach-run", identities={"run_id": plan.run_id, "attempt_id": plan.attempt_id},
                )
        finally:
            shutil.rmtree(private, ignore_errors=True)
        if response.get("status") != "ok" or type(response.get("payload")) is not dict:
            raise _failure("remote-operation-failed", "The submitter response is malformed.")
        record = {**record, "phase": "submitted", "launcher_bound": True, "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        self._write_run_record(record)

        return {
            "run_id": plan.run_id,
            "observation": self._bound_observation(record, response["payload"]),
            "upload_required": False,
        }

    def inspect_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        return (
            self._bound_observation(
                record, self._run_controller(record, "inspect-run", {})
            )
            if record["launcher_bound"]
            else self._allocated_observation(record)
        )

    def refresh_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        return (
            self._bound_observation(
                record, self._run_controller(record, "refresh-run", {})
            )
            if record["launcher_bound"]
            else self._allocated_observation(record)
        )

    def read_progress(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id", "after_sequence", "limit"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"]:
            return {**self._allocated_observation(record), "events": [], "has_more": False, "next_sequence": value["after_sequence"]}
        return self._bound_observation(
            record,
            self._run_controller(
                record,
                "read-progress",
                {"after_sequence": value["after_sequence"], "limit": value["limit"]},
            ),
        )

    def cancel_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"] and record["phase"] not in {"scheduler-intent", "uncertain"}:
            if record["phase"] != "cancelled":
                record = {**record, "phase": "cancelled", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                self._write_run_record(record)
            return self._allocated_observation(record)
        if not record["launcher_bound"]:
            raise _failure("submission-uncertain", "Cancellation cannot safely identify an uncertain scheduler job.", phase="cancellation", allocation_state="unknown", retry_safety="unsafe", next_action="inspect-scheduler", identities={"run_id": record["run_id"]})
        return self._bound_observation(
            record, self._run_controller(record, "cancel-run", {})
        )

    def plan_retry(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id", "recompute"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"]:
            raise _failure("retry-conflict", "Only a submitted terminal run can be retried.")
        return self._run_controller(record, "plan-retry", {"recompute": value["recompute"]})

    def start_retry(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"plan"})
        try:
            retry = parse_retry_plan(value["plan"])
        except (TypeError, ValueError) as exc:
            raise _failure("invalid-retry", "The retained retry plan is invalid.") from exc
        parent = self._read_run_record(retry.parent_run_id)
        observation = self._run_controller(parent, "start-retry", {"plan": retry.to_dict()})
        run_id = observation.get("run_id")
        if run_id != retry.retry_run_id:
            raise _failure("operation-record-tampered", "The retry changed its run binding.")
        run_dir = self._run_directory(run_id)
        if not run_dir.exists():
            run_dir.mkdir(mode=0o700)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            cloned = {
                **parent,
                "run_id": run_id,
                "attempt_id": str(uuid.uuid4()),
                "request_digest": canonical_digest({"retry_plan": retry.to_dict()}),
                "phase": "submitted",
                "launcher_bound": True,
                "revision": 0,
                "updated_at": now,
            }
            self._write_run_record(cloned)
        return self._bound_observation(self._read_run_record(run_id), observation)

    def prepare_result(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"]:
            raise _failure("result-integrity-failed", "The allocated run has no result.")
        return self._run_controller(record, "prepare-result", {})



__all__ = ["GatewayRunMixin"]
