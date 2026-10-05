"""Source-disabled Core artifact capability checks, not a full backend/tool suite.

Run from outside the checkout using an isolated interpreter with the built Core
wheel installed normally. Each child uses the same interpreter and public Core
task/result entry points; no source-package injection or private tracker APIs.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import zipfile
from dataclasses import replace
from importlib.metadata import distribution, version
from pathlib import Path


TOOL_SOURCE = '''import numpy as np
from bioimageflow_core import IOModel, ProcessingTool, RowConsumption, SharedArray
from bioimageflow_core.shm import create_shared_output, open_shared_array

class ArrayLifetimeTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED

    class Inputs(IOModel):
        reference: SharedArray
        fail: bool

    class Outputs(IOModel):
        reference: SharedArray
        total: int

    def process_row(self, arguments):
        with open_shared_array(arguments.reference) as array:
            assert array.dtype == np.dtype("uint16")
            np.testing.assert_array_equal(array, np.arange(6, dtype="uint16").reshape(2, 3))
            assert not array.flags.writeable
            try:
                array[0, 0] = 100
            except ValueError:
                pass
            else:
                raise AssertionError("accepted input was writable")
            total = int(array.sum())
        del array
        with create_shared_output(np.arange(6, dtype="uint16").reshape(2, 3) + 10) as output:
            pass
        if arguments.fail:
            raise RuntimeError("owned failure after allocation")
        return self.Outputs(reference=output, total=total)
'''


def artifact_identity(wheel: Path, source_root: Path, expected_version: str) -> dict:
    """Check every installed Python/typing byte against the selected wheel."""
    import bioimageflow_core as core

    dist = distribution("bioimageflow-core")
    module = Path(core.__file__).resolve()
    assert dist.version == expected_version, (dist.version, expected_version)
    assert module == Path(str(dist.locate_file("bioimageflow_core/__init__.py"))).resolve()
    source_packages = (source_root / "packages", source_root / "bioimageflow_core")
    assert not any(module.is_relative_to(source) for source in source_packages), (module, source_root)
    assert not Path.cwd().resolve().is_relative_to(source_root), Path.cwd()
    direct_url = dist.read_text("direct_url.json")
    if direct_url:
        assert not json.loads(direct_url).get("dir_info", {}).get("editable", False)
    hashes = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("bioimageflow_core/") and (name.endswith(".py") or name.endswith("/py.typed")):
                installed = Path(str(dist.locate_file(name))).resolve()
                assert not any(installed.is_relative_to(source) for source in source_packages), installed
                content = archive.read(name)
                assert installed.read_bytes() == content, name
                hashes[name] = hashlib.sha256(content).hexdigest()
    assert "bioimageflow_core/__init__.py" in hashes
    actual = {
        "bioimageflow_core/" + file.relative_to(module.parent).as_posix()
        for file in module.parent.rglob("*")
        if file.is_file() and (file.suffix == ".py" or file.name == "py.typed")
    }
    assert actual == set(hashes), (actual - set(hashes), set(hashes) - actual)
    return {
        "version": dist.version, "module": str(module), "module_hashes": hashes,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "python": platform.python_version(), "numpy": version("numpy"),
        "platform": platform.platform(), "pid": os.getpid(),
    }


def child(args: argparse.Namespace) -> None:
    from bioimageflow_core.worker import execute_processing_task

    identity = artifact_identity(args.wheel, args.source_root, args.expected_version)
    payload = json.loads(args.child_request.read_text(encoding="utf-8"))
    try:
        result = {"result": execute_processing_task(payload)}
    except RuntimeError as error:
        # The one admitted failure case is asserted by the controller. Any other
        # exception or message remains a failed capability check.
        if str(error) != "owned failure after allocation":
            raise
        result = {"expected_failure": str(error)}
    args.child_reply.write_text(json.dumps({**result, "identity": identity}), encoding="utf-8")


def refused(action, error_type, message: str = "") -> None:
    try:
        action()
    except error_type as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("Required refusal did not occur")


def run_child(command: list, log: Path, retirement: dict) -> None:
    """Return only after physical retirement, including interrupted waits."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=60)
    except BaseException:
        process.kill()
        stdout, stderr = process.communicate()
        retirement["physical_exit"] = True
        log.write_text(stdout + stderr, encoding="utf-8")
        raise
    retirement["physical_exit"] = True
    log.write_text(stdout + stderr, encoding="utf-8")
    assert process.returncode == 0, (process.returncode, stdout, stderr)


def transport_case(args: argparse.Namespace, owner, input_ref, source: Path, *, fail: bool):
    from bioimageflow_core import (
        ProcessingTask, RowInvocation, SourceFileOriginV1, collect_input_scopes,
        decode_processing_result, encode_processing_task, validate_processing_result,
        describe_tool_declaration,
    )

    from bioimageflow_core.worker_origins import load_worker_tool

    label = "failure" if fail else "success"
    input_ref = owner.publish(input_ref)
    task_scope = owner.task_scope(label)
    grant = task_scope.acquire_worker_grant(inputs=(input_ref,))
    task = ProcessingTask(
        task_id="task_0000000000000001", node_name="array-lifetime",
        invocation_id="inv_00000000000000000000000000000001",
        cache_attempt_id=None, task_retry=0, mode="row_chunk", row_consumption="mapped",
        tool=SourceFileOriginV1(path=str(source), source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
                              class_name="ArrayLifetimeTool"),
        declaration=describe_tool_declaration(load_worker_tool(SourceFileOriginV1(
            path=str(source), source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            class_name="ArrayLifetimeTool"))),
        rows=(RowInvocation(position=0, row_index="sample",
                            arguments={"reference": input_ref, "fail": fail}, context=None),),
        shared_memory_context={"output": task_scope.descriptor(), "inputs": list(collect_input_scopes((input_ref,)))},
    )
    request, reply = args.root / (label + "-request.json"), args.root / (label + "-reply.json")
    request.write_text(json.dumps(encode_processing_task(task)), encoding="utf-8")
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--wheel", str(args.wheel),
               "--source-root", str(args.source_root), "--expected-version", args.expected_version,
               "--child-request", str(request), "--child-reply", str(reply)]
    started = time.monotonic()
    retirement = {"physical_exit": False}
    try:
        run_child(command, args.root / (label + "-child.log"), retirement)
        decoded = json.loads(reply.read_text(encoding="utf-8"))
        assert decoded["identity"]["pid"] != os.getpid()
        assert decoded["identity"]["module_hashes"] == args.host_identity["module_hashes"]
        if fail:
            assert decoded["expected_failure"] == "owned failure after allocation"
            task_scope.close()
            assert task_scope.status().state == "pending"
            output = None
        else:
            value = decode_processing_result(decoded["result"])
            validate_processing_result(task, value)
            accepted = task_scope.accept_result(value.groups[0].outputs[0])
            assert accepted["total"] == 15
            output = accepted["reference"]
    finally:
        # run_child reaps the real child before returning/raising. No logical
        # task state is substituted for physical retirement.
        if retirement["physical_exit"]:
            try:
                grant.drained()
            except BaseException as error:
                args.grant_errors.append(repr(error))
        else:
            args.grant_errors.append("Child physical retirement was not established; grant retained")
    assert not args.grant_errors, args.grant_errors
    if fail:
        assert task_scope.status().state == "closed"
        assert not Path(task_scope.descriptor()["root"]).exists()
    else:
        task_scope.discard_unreturned()
    return output, {"pid": decoded["identity"]["pid"], "duration_seconds": time.monotonic() - started,
                    "physical_exit_before_grant_drain": True, "expected_failure": fail}


def capability_checks(args: argparse.Namespace) -> list:
    import numpy as np
    from bioimageflow_core import SharedArray, SharedMemoryContext
    from bioimageflow_core.shm import create_shared_output, open_shared_array

    owner = SharedMemoryContext(args.root / "owned", max_bytes=1024 * 1024)
    other = SharedMemoryContext(args.root / "other", max_bytes=4096)
    budget = SharedMemoryContext(args.root / "budget", max_bytes=200)
    outside = args.root / "outside.txt"
    outside.write_bytes(b"protected")
    cases = []
    try:
        with owner.activate(), create_shared_output(np.arange(6, dtype="uint16").reshape(2, 3)) as ref:
            pass
        source = args.root / "array_tool.py"
        source.write_text(TOOL_SOURCE, encoding="utf-8")
        output, child_receipt = transport_case(args, owner, ref, source, fail=False)
        assert isinstance(output, SharedArray)
        with open_shared_array(ref) as array:
            np.testing.assert_array_equal(array, [[0, 1, 2], [3, 4, 5]])
        del array
        with open_shared_array(output) as array:
            np.testing.assert_array_equal(array, [[10, 11, 12], [13, 14, 15]])
        del array
        cases.append({"family": "both-direction-physical-child-lifetime", "result": "PASS", **child_receipt})

        # A failed child creates a separate task namespace. Only that unpublished
        # namespace is reclaimed, after its physical child exit; accepted output
        # and the controller's input remain readable.
        with owner.activate(), create_shared_output(np.arange(6, dtype="uint16").reshape(2, 3)) as failure_ref:
            pass
        _, failure_receipt = transport_case(args, owner, failure_ref, source, fail=True)
        with budget.activate():
            refused(lambda: budget.create(np.array([object()], dtype=object)), ValueError, "Python objects")
            refused(lambda: budget.create(np.zeros(100, dtype=np.float64)), ValueError, "budget")
        assert not list(Path(budget.descriptor()["root"]).rglob("*.npy"))
        assert budget.close().state == "closed"
        with open_shared_array(output) as array:
            assert array.sum() == 75
        del array
        cases.append({"family": "guard-and-unpublished-failure-cleanup", "result": "PASS", **failure_receipt})

        with other.activate(), create_shared_output(np.array([7, 8], dtype="int32")) as unrelated:
            pass
        refused(lambda: owner.release(replace(ref, name="../outside.txt")), ValueError)
        refused(lambda: owner.bind(replace(ref, scope_id="not-admitted")), ValueError)
        assert outside.read_bytes() == b"protected"
        with open_shared_array(unrelated) as array:
            np.testing.assert_array_equal(array, [7, 8])
        del array
        cases.append({"family": "contained-owner-release", "result": "PASS"})

        with open_shared_array(output) as root_view:
            sliced = root_view[1:]
            retained = np.asarray(sliced)
        del root_view, sliced
        gc.collect()
        assert owner.close().state == "pending"
        np.testing.assert_array_equal(retained, [[13, 14, 15]])
        refused(lambda: owner.open(output), RuntimeError, "clos")
        del retained
        gc.collect()
        assert owner.status().state == "closed"
        assert not Path(owner.descriptor()["root"]).exists()
        assert outside.read_bytes() == b"protected"
        with open_shared_array(unrelated) as array:
            np.testing.assert_array_equal(array, [7, 8])
        del array
        assert other.close().state == "closed"
        cases.append({"family": "retained-derived-view-explicit-release", "result": "PASS"})
        return cases
    finally:
        # Public release only; never delete arbitrary paths or force-close a view.
        args.cleanup = []
        for context in (owner, other, budget):
            try:
                args.cleanup.append(vars(context.close()))
            except BaseException as error:
                # Cleanup evidence cannot replace an earlier capability failure.
                args.cleanup.append({"state": "pending", "errors": [repr(error)]})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expected-version", default="0.5.0")
    parser.add_argument("--expected-python")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--child-request", type=Path)
    parser.add_argument("--child-reply", type=Path)
    args = parser.parse_args()
    args.wheel, args.source_root = args.wheel.resolve(), args.source_root.resolve()
    if args.child_request:
        child(args)
        return 0
    if args.root is None or args.receipt is None:
        parser.error("controller requires --root and --receipt")
    args.root, args.receipt = args.root.resolve(), args.receipt.resolve()
    args.root.mkdir(parents=True, exist_ok=False)
    args.cleanup = []
    args.grant_errors = []
    started = time.monotonic()
    record = {"result": "FAIL", "scope": "Core artifact public-API child-process capabilities; not all tools/backends",
              "skipped": [], "cases": []}
    try:
        if args.expected_python:
            assert platform.python_version().startswith(args.expected_python + "."), platform.python_version()
        args.host_identity = artifact_identity(args.wheel, args.source_root, args.expected_version)
        record["artifact"] = args.host_identity
        record["cases"] = capability_checks(args)
        assert all(status["state"] == "closed" for status in args.cleanup), args.cleanup
        record["result"] = "PASS"
    except BaseException:
        record["traceback"] = traceback.format_exc()
        raise
    finally:
        record["cleanup"] = args.cleanup
        record["grant_errors"] = args.grant_errors
        record["duration_seconds"] = time.monotonic() - started
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print("Four Core artifact lifetime families PASS; 0 skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
