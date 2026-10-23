"""Finite selected Complete-runtime gates; not model accuracy certification."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class RuntimeGate:
    selector: str
    package: str
    tool: str
    project: str
    imports: tuple[str, ...] = ()
    cases: tuple[str, ...] = ()
    additional_selectors: tuple[str, ...] = ()
    identities: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int | None = None


_SEGMENTATION = "packages/bioimageflow-segmentation-tools/tests/"
_TASK_API = "tests/integration/test_wetlands_task_api.py"
_CANCELLATION = "tests/integration/test_wetlands_cancellation.py"
_WETLANDS_SMOKE = "tests/integration/test_wetlands_smoke.py"
_COMMON_WETLANDS = "packages/bioimageflow-common-tools/tests/test_common_complete_wetlands.py"
_TASK_API_IDENTITIES = tuple(
    ("tests.integration.test_wetlands_task_api." + classname, name)
    for classname, names in (
        ("TestRowParallelism", (
            "test_single_worker_produces_correct_results", "test_multi_worker_same_results",
            "test_row_ordering_preserved", "test_error_propagation_with_cleanup", "test_batch_tool_via_submit",
        )),
        ("TestWetlandsResourceRequirements", (
            "test_gpu_tool_runs_without_mutating_worker_environment", "test_get_environment_override",
            "test_workflow_level_max_workers_default",
        )),
        ("TestSubRowProgress", (
            "test_row_progress_events_emitted", "test_failed_event_on_error", "test_batch_progress_events",
        )),
        ("TestBranchParallelism", (
            "test_independent_branches_concurrent", "test_results_identical_to_sequential",
            "test_dataframe_tool_never_concurrent", "test_sequential_engine_deterministic",
        )),
    )
    for name in names
)
MODEL_GATES = ("stardist", "instanseg", "nagini-api", "laptrack")
WETLANDS_GATES = ("wetlands-local", "wetlands-published")
GATES = {
    "stardist": RuntimeGate(
        _SEGMENTATION + "test_execution.py::test_stardist_runtime_segments_tiny_synthetic_image",
        "bioimageflow_segmentation_tools", "StarDistSegmenter",
        "packages/bioimageflow-segmentation-tools",
    ),
    "instanseg": RuntimeGate(
        _SEGMENTATION + "test_instanseg_runtime.py::test_real_instanseg_named_model_inference",
        "bioimageflow_segmentation_tools", "InstanSegSegment",
        "packages/bioimageflow-segmentation-tools", ("instanseg",),
    ),
    "nagini-api": RuntimeGate(
        _SEGMENTATION + "test_nagini_runtime.py::test_real_nagini_runtime_exposes_adapter_api",
        "bioimageflow_segmentation_tools", "Nagini3DSegment",
        "packages/bioimageflow-segmentation-tools",
        ("nagini3D.models.model", "nagini3D.models.tools.snake_sampler"),
    ),
    "laptrack": RuntimeGate(
        "packages/bioimageflow-tracking-tools/tests/test_laptrack_runtime.py"
        "::test_real_laptrack_links_two_deterministic_tracks",
        "bioimageflow_tracking_tools", "LapTrackLink",
        "packages/bioimageflow-tracking-tools", ("laptrack",),
    ),
    "sairpico": RuntimeGate(
        "packages/bioimageflow-sairpico-tools/tests/test_sairpico_complete_binary_tools.py"
        "::test_exported_sairpico_binary_tool_executes_real_cli",
        "", "", "",
        cases=("gaussian-psf", "gibson-lanni-psf", "richardson-lucy-deconvolution",
               "wiener-deconvolution", "spitfire-deconvolution", "median-denoising",
               "cimg-denoising", "hotspot-detection"),
    ),
    "wetlands-local": RuntimeGate(
        _TASK_API, "", "", "",
        additional_selectors=(
            _CANCELLATION + "::test_cancel_reaches_active_rows_and_drains_submitted_window",
            _CANCELLATION + "::test_cancel_reaches_active_batch_and_drains_it",
            _WETLANDS_SMOKE + "::test_configured_logging_routes_wetlands_worker_streams",
            _COMMON_WETLANDS + "::test_connected_components_labels_binary_objects_through_wetlands",
        ),
        identities=_TASK_API_IDENTITIES + (
            ("tests.integration.test_wetlands_cancellation", "test_cancel_reaches_active_rows_and_drains_submitted_window"),
            ("tests.integration.test_wetlands_cancellation", "test_cancel_reaches_active_batch_and_drains_it"),
            ("tests.integration.test_wetlands_smoke", "test_configured_logging_routes_wetlands_worker_streams"),
            ("packages.bioimageflow-common-tools.tests.test_common_complete_wetlands",
             "test_connected_components_labels_binary_objects_through_wetlands"),
        ),
        timeout_seconds=3600,
    ),
    "wetlands-published": RuntimeGate(
        _WETLANDS_SMOKE + "::test_processing_tool_executes_through_published_wetlands",
        "", "", "", timeout_seconds=1200,
    ),
}
RUNTIME_CHOICES = ("all", *MODEL_GATES, *WETLANDS_GATES)
SUITES = ("all", "wetlands", "public-data", "external-binaries", "model-runtimes")


def validate_selection(suite: str, gate: str, event: str = "workflow_dispatch") -> None:
    if suite not in SUITES or gate not in RUNTIME_CHOICES:
        raise ValueError("Unknown Complete suite or runtime gate")
    if event not in ("schedule", "workflow_dispatch"):
        raise ValueError("Unsupported Complete event")
    if event == "schedule" and (suite, gate) != ("all", "all"):
        raise ValueError("Scheduled Complete validation retains all defaults")
    if gate in MODEL_GATES and suite != "model-runtimes":
        raise ValueError("A targeted model/runtime gate requires suite=model-runtimes")
    if gate in WETLANDS_GATES and suite != "wetlands":
        raise ValueError("A targeted Wetlands gate requires suite=wetlands")


def runtime_plan(gate: str) -> dict[str, Any]:
    if gate not in MODEL_GATES:
        raise ValueError("Expected one selected model/runtime gate")
    from bioimageflow_core import EnvironmentSpec

    selected = GATES[gate]
    tool = getattr(importlib.import_module(selected.package), selected.tool)
    environment = tool.environment
    if not isinstance(environment, EnvironmentSpec):
        raise ValueError("Selected runtime requires an EnvironmentSpec")
    recipe = environment.dependencies
    if set(recipe) != {"python", "pip"}:
        raise ValueError("Selected runtime gate requires the current finite pip recipe")
    python = recipe["python"]
    requirements = recipe["pip"]
    if (not isinstance(python, str) or re.fullmatch(r"[0-9]+\.[0-9]+", python) is None
            or not isinstance(requirements, list)):
        raise ValueError("Malformed selected runtime recipe")
    if not all(isinstance(value, str) and "\n" not in value and "\r" not in value
               for value in requirements):
        raise ValueError("Malformed selected runtime requirements")
    return {
        "gate": gate, "selector": selected.selector, "python": python,
        "project": selected.project, "direct": bool(selected.imports),
        "requirements": requirements, "environment_name": environment.name,
        "source_sha": os.environ.get("GITHUB_SHA"),
        "authority": "selected source/editable Tool.environment; managed worker or isolated direct runtime",
    }


def preflight(gate: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Check actual direct runtime pins/imports; managed host is not its worker."""
    if gate not in GATES or not GATES[gate].imports:
        raise ValueError("Direct preflight is only for the three isolated runtimes")
    if runtime_plan(gate) != plan:
        raise ValueError("Held selected runtime plan no longer matches its public recipe")
    if platform.python_version_tuple()[:2] != tuple(plan["python"].split(".")):
        raise ValueError("Interpreter does not match selected runtime Python")
    from packaging.requirements import Requirement

    packages = {}
    for text in plan["requirements"]:
        requirement = Requirement(text)
        if requirement.url or requirement.marker or len(requirement.specifier) != 1:
            raise ValueError("Selected gate requires its current unconditional exact pins")
        pin = next(iter(requirement.specifier))
        if pin.operator != "==" or "*" in pin.version:
            raise ValueError("Selected gate requires exact runtime versions")
        actual = version(requirement.name)
        if not requirement.specifier.contains(actual, prereleases=True):
            raise ValueError(f"Wrong runtime distribution: {requirement.name}=={actual}")
        packages[requirement.name] = actual
    modules = {
        name: str(importlib.import_module(name).__file__)
        for name in GATES[gate].imports
    }
    if gate == "instanseg" and not os.environ.get("INSTANSEG_BIOIMAGEIO_PATH"):
        raise ValueError("InstanSeg requires an explicitly owned model cache")
    import bioimageflow_core

    return {
        "gate": gate, "python": platform.python_version(), "executable": sys.executable,
        "packages": packages, "imports": modules, "plan": plan,
        "core_module": str(bioimageflow_core.__file__),
        "scope": "actual pinned imports; inference/API effects established separately by exact pytest case",
    }


def expected_identities(gate: str) -> tuple[tuple[str, str], ...]:
    if gate not in GATES:
        raise ValueError("Unknown runtime report gate")
    selected = GATES[gate]
    if selected.identities:
        return selected.identities
    filename, function = selected.selector.split("::")
    classname = filename.removesuffix(".py").replace("/", ".")
    names = tuple(function + f"[{case}]" for case in selected.cases) if selected.cases else (function,)
    return tuple((classname, name) for name in names)


def check_junit(gate: str, path: Path) -> dict[str, Any]:
    expected = set(expected_identities(gate))
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError("Expected a JUnit testsuite report")
    cases = list(root.iter("testcase"))
    identities = [(case.get("classname"), case.get("name")) for case in cases]
    if len(cases) != len(expected) or set(identities) != expected:
        raise ValueError("Runtime report does not contain exactly the selected case identities")
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise ValueError("A required runtime case failed or skipped")
    for suite in root.iter("testsuite"):
        if any(int(suite.get(name, "0")) != 0 for name in ("failures", "errors", "skipped")):
            raise ValueError("Runtime report includes failures, errors or skips")
    return {"gate": gate, "passed": len(cases), "failed": 0, "errors": 0,
            "skipped": 0, "cases": [name for _, name in identities],
            "case_identities": [{"classname": classname, "name": name} for classname, name in identities],
            "source_sha": os.environ.get("GITHUB_SHA")}


def run_pytest(gate: str, report: Path, output: Path) -> int:
    """Run only the fixed selector, rejecting a missing/skipped green report."""
    if gate not in GATES:
        raise ValueError("Unknown selected runtime")
    if gate == "wetlands-published" and "BIOIMAGEFLOW_CORE_SOURCE" in os.environ:
        raise ValueError("The published Wetlands gate refuses BIOIMAGEFLOW_CORE_SOURCE")
    selected = GATES[gate]
    bounds = {"timeout": selected.timeout_seconds} if selected.timeout_seconds is not None else {}
    result = subprocess.run([
        sys.executable, "-B", "-m", "pytest", selected.selector, *selected.additional_selectors,
        "--run-complete", "-p", "no:cacheprovider", "-rsx", f"--junitxml={report}",
    ], check=False, **bounds)
    receipt = check_junit(gate, report)
    _write_json(output, receipt)
    return result.returncode


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    selection = commands.add_parser("selection")
    selection.add_argument("--suite", required=True)
    selection.add_argument("--gate", required=True)
    selection.add_argument("--event", default="workflow_dispatch")
    plan = commands.add_parser("plan")
    plan.add_argument("--gate", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--requirements", type=Path, required=True)
    plan.add_argument("--github-output", type=Path, required=True)
    flight = commands.add_parser("preflight")
    flight.add_argument("--gate", required=True)
    flight.add_argument("--plan", type=Path, required=True)
    flight.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check-junit")
    check.add_argument("--gate", required=True)
    check.add_argument("--report", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--gate", required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "selection":
        validate_selection(args.suite, args.gate, args.event)
        return
    if args.command == "run":
        sys.exit(run_pytest(args.gate, args.report, args.output))
    if args.command == "plan":
        value = runtime_plan(args.gate)
        _write_json(args.output, value)
        args.requirements.write_text("\n".join(value["requirements"]) + "\n")
        with args.github_output.open("a") as output:
            for key in ("python", "project", "selector"):
                print(f"{key}={value[key]}", file=output)
            print(f"direct={str(value['direct']).lower()}", file=output)
    elif args.command == "preflight":
        value = preflight(args.gate, json.loads(args.plan.read_text()))
        _write_json(args.output, value)
    else:
        value = check_junit(args.gate, args.report)
        _write_json(args.output, value)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
