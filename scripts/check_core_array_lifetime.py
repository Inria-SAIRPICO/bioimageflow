"""Source-disabled Core contracts and array capabilities, not a full tool suite.

Run from outside the checkout using an isolated interpreter with the built Core
wheel installed normally. Each child uses the same interpreter and public Core
task/result entry points; no source-package injection or private tracker APIs.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
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


def source_tool_origin(source: Path):
    """Capture one current proof for the freshly generated trusted fixture."""
    from bioimageflow_core import SourceFileOrigin
    from bioimageflow_core.primary_content import capture_primary_content

    name = "_core_array_capability_tool"
    assert name not in sys.modules, "Fixture module already has a resident owner"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        admission = capture_primary_content(module.ArrayLifetimeTool)
        return SourceFileOrigin(
            path=str(source), source_hash=admission.source_hash(source),
            class_name="ArrayLifetimeTool", primary=admission.proof,
        )
    except BaseException:
        if sys.modules.get(name) is module:
            del sys.modules[name]
        raise


def transport_case(args: argparse.Namespace, owner, input_ref, origin, *, fail: bool):
    from bioimageflow_core import (
        ProcessingTask, RowInvocation, collect_input_scopes,
        decode_processing_result, encode_processing_task, validate_processing_result,
        describe_tool_declaration,
    )

    from bioimageflow_core.worker_origins import load_worker_tool

    label = "failure" if fail else "success"
    declaration = describe_tool_declaration(load_worker_tool(origin))
    input_ref = owner.publish(input_ref)
    task_scope = owner.task_scope(label)
    grant = task_scope.acquire_worker_grant(inputs=(input_ref,))
    task = ProcessingTask(
        task_id="task_0000000000000001", node_name="array-lifetime",
        invocation_id="inv_00000000000000000000000000000001",
        cache_attempt_id=None, task_retry=0, mode="row_chunk", row_consumption="mapped",
        tool=origin,
        declaration=declaration,
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


def core_contract_checks() -> list:
    """Exercise installed worker-floor APIs without importing the controller."""
    import importlib
    import pkgutil
    from typing import Annotated, List, Optional

    import bioimageflow_core as core
    from bioimageflow_core.defaults import snapshot_value
    from bioimageflow_core.worker_origins import load_worker_tool

    modules = sorted(item.name for item in pkgutil.walk_packages(core.__path__, core.__name__ + "."))
    for name in modules:
        importlib.import_module(name)
    assert "bioimageflow" not in sys.modules
    assert "pandas" not in sys.modules and "pydantic" not in sys.modules
    assert Path(core.__file__).with_name("py.typed").is_file()

    class ParentInputs(core.IOModel):
        CountType = Annotated[int, core.GUIMeta("Count", min=0)]
        ImagePath = Annotated[Path, core.ImageSpec(layouts={core.Layout.PLANAR}), core.GUIMeta("Pixels")]
        MaybePath = Optional[ImagePath]
        count: CountType = 2
        missing: int
        path: MaybePath = None

    class Inputs(ParentInputs):
        ValuesType = List[int]
        values: ValuesType = [1, 2]

    declaration = core.describe_io_model(Inputs)
    assert declaration["field_names"] == ["count", "missing", "path", "values"]
    assert declaration["fields"]["count"]["constraints"] == {"min": 0}
    assert declaration["fields"]["missing"]["required"]
    assert declaration["fields"]["path"]["nullable"]
    assert declaration["fields"]["path"]["image_spec"]["layouts"] == ["YX"]
    gui_meta = core.extract_gui_meta(ParentInputs.MaybePath)
    assert gui_meta is not None and gui_meta.display_name == "Pixels"
    constraints = {"semantics": {core.Semantic.INTENSITY}, "layouts": {core.Layout.PLANAR},
                   "formats": {"tiff"}, "dtypes": {"uint16"}}
    image_spec = core.ImageSpec(**constraints)
    equal_spec = core.ImageSpec(**{key: frozenset(value) for key, value in constraints.items()})
    captured_hash = hash(image_spec)
    for values in constraints.values():
        values.clear()
    assert image_spec == equal_spec and hash(image_spec) == hash(equal_spec) == captured_hash
    class AlternateInputs(core.IOModel):
        path: Annotated[Optional[Path], core.ImageSpec(layouts={core.Layout.PLANAR}), core.GUIMeta("Pixels")] = None
    assert core.describe_io_model(AlternateInputs)["fields"]["path"] == declaration["fields"]["path"]
    defaults = Inputs.capture_defaults()
    assert defaults == {"count": 2, "path": None, "values": [1, 2]}
    defaults["values"].append(9)
    assert Inputs.capture_defaults()["values"] == [1, 2]
    cases = [{"family": "installed-core-import-annotations-defaults", "result": "PASS", "modules": modules}]

    recipe = {"python": "3.9", "pip": ["sample==1.0"], "channels": ["conda-forge"],
              "local": [{"name": "sample", "path": Path("held-project"), "editable": True,
                         "extras": ["base"]}]}
    spec = core.EnvironmentSpec("floor-recipe", recipe)
    captured = snapshot_value(spec)
    assert captured is not spec and captured == spec
    recipe["local"][0]["extras"].append("original-edit")
    projected = captured.dependencies
    projected["local"][0]["extras"].append("projection-edit")
    projected["channels"].append("projection-edit")
    assert captured == spec
    assert spec.dependencies["local"][0]["extras"] == ["base"]
    assert captured.dependencies["channels"] == ["conda-forge"]
    cases.append({"family": "installed-core-detached-recipe-snapshot", "result": "PASS"})

    viewer = core.ViewerSpec(core.NapariRequirement(required_packages=[core.PackageRequirement("Example_Reader")]))
    wire = viewer.to_dict()
    assert core.ViewerSpec.from_dict(wire) == viewer
    assert viewer.napari is not None
    assert viewer.napari.required_packages[0].normalized_name == "example-reader"
    wire["napari"]["required_packages"] = ["Example_Reader"]
    refused(lambda: core.ViewerSpec.from_dict(wire), (TypeError, ValueError))
    cases.append({"family": "installed-core-strict-viewer-wire", "result": "PASS"})

    proof = core.capture_primary_content(core.ProcessingTool, distribution="bioimageflow-core").proof
    origin = core.InstalledModuleOrigin(
        distribution="bioimageflow-core", version=version("bioimageflow-core"),
        module=core.ProcessingTool.__module__, class_name=core.ProcessingTool.__name__, primary=proof,
    )
    payload = core.encode_worker_tool_origin(origin)
    assert payload["schema"] == "bioimageflow.worker_tool_origin.v2"
    assert core.decode_worker_tool_origin(payload) == origin
    assert tuple(callback.role for callback in proof.callbacks) == ("__new__", "__init__", "process_row", "process_batch")
    instance = load_worker_tool(origin)
    assert type(instance) is core.ProcessingTool and load_worker_tool(origin) is instance
    payload["primary"]["callbacks"] = []
    refused(lambda: core.decode_worker_tool_origin(payload), ValueError)
    cases.append({"family": "installed-core-current-primary-admission", "result": "PASS",
                  "schema": origin.schema, "members": len(proof.members)})
    return cases


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
        origin = source_tool_origin(source)
        output, child_receipt = transport_case(args, owner, ref, origin, fail=False)
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
        _, failure_receipt = transport_case(args, owner, failure_ref, origin, fail=True)
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
    record = {"result": "FAIL", "scope": "Core artifact public contracts and child-process capabilities; not all tools/backends",
              "skipped": [], "cases": []}
    try:
        if args.expected_python:
            assert platform.python_version().startswith(args.expected_python + "."), platform.python_version()
        args.host_identity = artifact_identity(args.wheel, args.source_root, args.expected_version)
        record["artifact"] = args.host_identity
        record["cases"] = core_contract_checks()
        record["cases"].extend(capability_checks(args))
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
    print("Eight Core artifact contract/lifetime families PASS; 0 skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
