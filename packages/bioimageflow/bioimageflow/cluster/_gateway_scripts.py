"""Embedded child programs installed into managed deployments."""

_ATTESTATION_SCRIPT = r'''from __future__ import annotations
import importlib.metadata as metadata
import hashlib
import json
import os
import platform
import sys

required = ("bioimageflow", "bioimageflow-core", "parsl", "psij-python")
packages = {}
distribution_metadata = {}
missing = []
for name in required:
    try:
        distribution = metadata.distribution(name)
        packages[name] = distribution.version
        hashes = {}
        for item in distribution.files or ():
            if item.name not in {"METADATA", "RECORD"}:
                continue
            digest = hashlib.sha256()
            size = 0
            with distribution.locate_file(item).open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise RuntimeError("distribution metadata exceeds attestation limit")
                    digest.update(chunk)
            key = item.name.lower()
            hashes[key + "_sha256"] = "sha256:" + digest.hexdigest()
            hashes[key + "_size"] = size
        distribution_metadata[name] = hashes
    except metadata.PackageNotFoundError:
        missing.append(name)
try:
    import psij
    executor_names = sorted(psij.JobExecutor.get_executor_names())
except Exception:
    executor_names = []
psij_distributions = {}
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name") or ""
    if "psij" in name.lower():
        psij_distributions[name] = distribution.version
value = {
    "schema": "bioimageflow.cluster.existing_python_attestation.v1",
    "requested_executable": os.environ["BIOIMAGEFLOW_REQUESTED_PYTHON"],
    "resolved_executable": os.path.realpath(sys.executable),
    "implementation": sys.implementation.name,
    "cache_tag": sys.implementation.cache_tag,
    "version": platform.python_version(),
    "version_info": list(sys.version_info[:3]),
    "platform_system": platform.system(),
    "platform_machine": platform.machine(),
    "packages": packages,
    "distribution_metadata": distribution_metadata,
    "missing_packages": sorted(missing),
    "psij_executor_names": executor_names,
    "psij_distributions": dict(sorted(psij_distributions.items())),
}
print(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
'''

_FACTORY_VALIDATOR_SCRIPT = r'''from __future__ import annotations
import json
import os
import sys
from pathlib import Path, PurePosixPath


def main():
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_bytes())
    encoded_secrets = sys.stdin.buffer.read(256 * 1024 + 1)
    if len(encoded_secrets) > 256 * 1024:
        raise RuntimeError("secret handoff exceeds validation limit")
    secret_values = json.loads(encoded_secrets or b"{}")
    if not isinstance(secret_values, dict):
        raise RuntimeError("secret handoff must be an object")
    content = Path(request["content_root"])
    parsl_root = content / "parsl"
    sys.path.insert(0, str(parsl_root))
    include = parsl_root / "include"
    if include.is_dir():
        for child in sorted(include.iterdir()):
            if child.is_dir():
                sys.path.insert(0, str(child))
    from bioimageflow.parsl.factory import ParslFactoryRuntime, managed_worker_init
    from bioimageflow.parsl.managed_validation import validate_managed_factory
    from bioimageflow.parsl.startup import CORE_REQUIREMENT

    factory = request["parsl"]
    reference = (
        "factory:" + factory["factory"]
        if factory["source_kind"] == "file"
        else factory["source"]
    )
    deployment_root = PurePosixPath(request["deployment_root"])
    setup = content / "setup" / "setup.sh"
    runtime = ParslFactoryRuntime(
        deployment_root=deployment_root,
        deployment_id=request["deployment_id"],
        worker_init=managed_worker_init(
            setup_path=PurePosixPath(str(setup)) if setup.is_file() else None,
            activation_path=deployment_root / "activation.sh",
            deployment_id=request["deployment_id"],
        ),
        environment_name=request["environment_name"],
        environment_identity=request["attestation_digest"],
        core_requirement=CORE_REQUIREMENT,
    )
    report = validate_managed_factory(
        reference,
        runtime=runtime,
        orchestrator_scheduler=request["scheduler"],
        kwargs=factory["kwargs"],
        secret_refs=factory["secret_refs"],
        secret_values=secret_values,
        timeout=request["timeout"],
    )
    secret_values.clear()
    print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''

_RUN_SUBMITTER_SCRIPT = r'''from __future__ import annotations
import json
import sys
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_bytes())
    content = Path(request["deployment_content"])
    parsl_root = content / "parsl"
    sys.path.insert(0, str(parsl_root))
    include = parsl_root / "include"
    if include.is_dir():
        for child in sorted(include.iterdir()):
            if child.is_dir():
                sys.path.insert(0, str(child))

    import bioimageflow.launcher.submission as submission_module
    from bioimageflow.launcher.cluster_submit import _load_inputs, _load_node_input_overrides
    from bioimageflow.launcher.payload import load_workflow_payload
    from bioimageflow.launcher.pre_launch import PreLaunchScript
    from bioimageflow.launcher.remote_control import inspect_run
    from bioimageflow.launcher.submission import _submit_workflow
    from bioimageflow.launcher.types import ParslConfigRef, PSIJLaunchConfig
    from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy

    # Secrets are resolved by the gateway into the run-private pre-launch
    # handoff. They must not enter this submitter process environment.
    submission_module.verify_secret_references = lambda _reference: None
    invocation_root = Path(request["invocation_root"])
    invocation = json.loads((invocation_root / "invocation.json").read_bytes())
    workflow = load_workflow_payload(
        invocation["workflow"], storage_path=Path(request["storage_path"])
    )
    _load_node_input_overrides(
        invocation_root, workflow, invocation["node_input_overrides"]
    )
    inputs = _load_inputs(invocation_root, workflow, invocation["inputs"])
    run = _submit_workflow(
        workflow,
        inputs=inputs if invocation["targets"] is None else None,
        targets=invocation["targets"],
        parsl_config=ParslConfigRef.from_dict(request["parsl_config"]),
        executor_bindings={
            label: ExecutorBinding.from_dict(value)
            for label, value in request["executor_bindings"].items()
        },
        node_routes=request["node_routes"],
        environment_routes={},
        shared_runtime_root=Path(request["shared_runtime_root"]),
        task_policy=ParslTaskPolicy.from_dict(invocation["task_policy"]),
        launch=PSIJLaunchConfig.from_dict(request["launch"]),
        pre_launch=PreLaunchScript.from_text(request["pre_launch"]),
        preallocated_run_id=request["run_id"],
        preserve_cluster_paths=True,
    )
    print(json.dumps({
        "status": "ok",
        "payload": inspect_run(request["storage_path"], run.id),
    }, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''

_RUN_CONTROLLER_SCRIPT = r'''from __future__ import annotations
import json
import sys
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_bytes())
    from bioimageflow.launcher.cluster_protocol import ClusterProtocolFailure
    from bioimageflow.launcher.remote_control import (
        cancel_run, inspect_run, plan_run_retry, read_progress_page,
        refresh_run, start_run_retry,
    )
    from bioimageflow.launcher.result_bundle import prepare_result
    operation = request["operation"]
    arguments = request["arguments"]
    storage = request["storage_path"]
    run_id = request["run_id"]
    try:
        if operation == "inspect-run":
            payload = inspect_run(storage, run_id)
        elif operation == "refresh-run":
            payload = refresh_run(storage, run_id)
        elif operation == "read-progress":
            payload = read_progress_page(
                storage, run_id, arguments["after_sequence"], arguments["limit"]
            )
        elif operation == "cancel-run":
            payload = cancel_run(
                request["transfer_root"], storage, run_id,
                request["request_id"], request["request_digest"],
            )
        elif operation == "plan-retry":
            payload = plan_run_retry(storage, run_id, arguments["recompute"])
        elif operation == "start-retry":
            payload = start_run_retry(storage, arguments["plan"])
        elif operation == "prepare-result":
            payload = prepare_result(
                request["transfer_root"], storage, run_id,
                request["request_id"], request["request_digest"],
            )
        else:
            raise RuntimeError("unsupported managed run operation")
    except ClusterProtocolFailure as exc:
        print(json.dumps({"status": "error", "category": exc.code, "message": exc.message}, sort_keys=True, separators=(",", ":")))
        return
    print(json.dumps({"status": "ok", "payload": payload}, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''

__all__ = [
    "_ATTESTATION_SCRIPT",
    "_FACTORY_VALIDATOR_SCRIPT",
    "_RUN_CONTROLLER_SCRIPT",
    "_RUN_SUBMITTER_SCRIPT",
]
