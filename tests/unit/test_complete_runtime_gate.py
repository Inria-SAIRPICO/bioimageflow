"""Selected release runtimes require exact effects rather than skipped green runs."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from tests.unit.test_release_tooling import _workflow


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "complete_runtime_gate", ROOT / "scripts/check_complete_runtime_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _report(path, selected, *, effect=None, names=None, classname=None, identities=None):
    expected = gate.expected_identities(selected)
    if identities is None:
        identities = expected if names is None else [(expected[0][0], name) for name in names]
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests=str(len(expected)))
    for module, name in identities:
        case = ET.SubElement(
            suite, "testcase", name=name,
            classname=classname or module,
        )
        if effect:
            ET.SubElement(case, effect)
    ET.ElementTree(root).write(path)
    return path


def test_complete_selection_keeps_defaults_and_refuses_incompatible_target():
    gate.validate_selection("all", "all", "schedule")
    gate.validate_selection("external-binaries", "all")
    for selected in gate.MODEL_GATES:
        gate.validate_selection("model-runtimes", selected)
        with pytest.raises(ValueError):
            gate.validate_selection("all", selected)
        with pytest.raises(ValueError):
            gate.validate_selection("model-runtimes", selected, "schedule")
    for selected in gate.WETLANDS_GATES:
        gate.validate_selection("wetlands", selected)
        for suite in ("all", "model-runtimes", "external-binaries", "public-data"):
            with pytest.raises(ValueError):
                gate.validate_selection(suite, selected)
        with pytest.raises(ValueError):
            gate.validate_selection("wetlands", selected, "schedule")
    with pytest.raises(ValueError):
        gate.validate_selection("model-runtimes", "arbitrary-selector")


def test_complete_workflow_validates_before_resources_and_keeps_selected_recipe_authority():
    workflow = _workflow(ROOT, "complete.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["runtime_gate"]["default"] == "all"
    assert inputs["runtime_gate"]["options"] == list(gate.RUNTIME_CHOICES)
    assert "inputs.suite" in workflow["concurrency"]["group"]
    assert "inputs.runtime_gate" in workflow["concurrency"]["group"]
    for name, job in workflow["jobs"].items():
        if name != "selection":
            assert job["needs"] == "selection"
    steps = workflow["jobs"]["model-runtimes"]["steps"]
    scripts = "\n".join(step.get("run", "") for step in steps)
    assert "runtime-requirements.txt" in scripts and "--requirements" in scripts
    assert "steps.runtime.outputs.python" in scripts
    assert "preflight" in scripts and "check_complete_runtime_gate.py run" in scripts
    assert "--run-complete" in scripts  # Retained all-runtime lane.
    for selected in gate.MODEL_GATES:
        plan = gate.runtime_plan(selected)
        tool = getattr(__import__(gate.GATES[selected].package), gate.GATES[selected].tool)
        assert plan["requirements"] == tool.environment.dependencies["pip"]
        assert plan["python"] == tool.environment.dependencies["python"]
        assert plan["direct"] is (selected != "stardist")


@pytest.mark.parametrize("selected", ["instanseg", "sairpico"])
def test_complete_junit_requires_all_exact_case_identities(tmp_path, selected):
    report = _report(tmp_path / "junit.xml", selected)
    result = gate.check_junit(selected, report)
    assert result["passed"] == (8 if selected == "sairpico" else 1)
    assert result["failed"] == result["errors"] == result["skipped"] == 0


@pytest.mark.parametrize("effect", ["skipped", "failure", "error"])
def test_complete_junit_refuses_missing_runtime_effect(tmp_path, effect):
    report = _report(tmp_path / "junit.xml", "laptrack", effect=effect)
    with pytest.raises(ValueError):
        gate.check_junit("laptrack", report)


def test_complete_junit_refuses_wrong_or_duplicate_cases(tmp_path):
    report = tmp_path / "junit.xml"
    function = gate.GATES["laptrack"].selector.split("::")[1]
    for names, classname in [
        ([], None), ([function, function], None), (["another_test"], None),
        ([function], "other.module"),
    ]:
        _report(report, "laptrack", names=names, classname=classname)
        with pytest.raises(ValueError):
            gate.check_junit("laptrack", report)


def test_complete_launcher_uses_fixed_selector_and_checks_report(tmp_path, monkeypatch):
    report, receipt = tmp_path / "junit.xml", tmp_path / "receipt.json"
    calls = []

    def run(command, *, check):
        calls.append(command)
        assert check is False
        _report(report, "laptrack")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.run_pytest("laptrack", report, receipt) == 0
    assert calls[0][:4] == [sys.executable, "-B", "-m", "pytest"]
    assert calls[0][4] == gate.GATES["laptrack"].selector
    assert "--run-complete" in calls[0]
    assert json.loads(receipt.read_text())["passed"] == 1
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    _report(report, "laptrack", effect="skipped")
    with pytest.raises(ValueError):
        gate.run_pytest("laptrack", report, receipt)


def test_complete_direct_preflight_checks_pins_without_certifying_managed_host(monkeypatch):
    plan = {"python": "3.11", "requirements": ["laptrack==0.17.1"]}
    monkeypatch.setattr(gate, "runtime_plan", lambda selected: plan)
    monkeypatch.setattr(gate.platform, "python_version_tuple", lambda: ("3", "11", "9"))
    monkeypatch.setattr(gate, "version", lambda name: "0.17.1")
    original_import = gate.importlib.import_module
    monkeypatch.setattr(gate.importlib, "import_module", lambda name: (
        SimpleNamespace(__file__="/owned/runtime/laptrack.py")
        if name == "laptrack" else original_import(name)
    ))
    assert gate.preflight("laptrack", plan)["packages"] == {"laptrack": "0.17.1"}
    monkeypatch.setattr(gate, "version", lambda name: "0.17.0")
    with pytest.raises(ValueError, match="Wrong runtime"):
        gate.preflight("laptrack", plan)
    with pytest.raises(ValueError, match="Direct preflight"):
        gate.preflight("stardist", plan)


def test_wetlands_gates_keep_finite_case_sets_separate_from_models():
    local = gate.GATES["wetlands-local"]
    assert (local.selector, *local.additional_selectors) == (
        "tests/integration/test_wetlands_task_api.py",
        "tests/integration/test_wetlands_cancellation.py::test_cancel_reaches_active_rows_and_drains_submitted_window",
        "tests/integration/test_wetlands_cancellation.py::test_cancel_reaches_active_batch_and_drains_it",
        "tests/integration/test_wetlands_smoke.py::test_configured_logging_routes_wetlands_worker_streams",
        "packages/bioimageflow-common-tools/tests/test_common_complete_wetlands.py"
        "::test_connected_components_labels_binary_objects_through_wetlands",
    )
    identities = gate.expected_identities("wetlands-local")
    assert len(identities) == len(set(identities)) == 19
    task_api = {(module.rsplit(".", 1)[1], name) for module, name in identities
                if module.startswith("tests.integration.test_wetlands_task_api.")}
    assert task_api == {
        ("TestRowParallelism", "test_single_worker_produces_correct_results"),
        ("TestRowParallelism", "test_multi_worker_same_results"),
        ("TestRowParallelism", "test_row_ordering_preserved"),
        ("TestRowParallelism", "test_error_propagation_with_cleanup"),
        ("TestRowParallelism", "test_batch_tool_via_submit"),
        ("TestWetlandsResourceRequirements", "test_gpu_tool_runs_without_mutating_worker_environment"),
        ("TestWetlandsResourceRequirements", "test_get_environment_override"),
        ("TestWetlandsResourceRequirements", "test_workflow_level_max_workers_default"),
        ("TestSubRowProgress", "test_row_progress_events_emitted"),
        ("TestSubRowProgress", "test_failed_event_on_error"),
        ("TestSubRowProgress", "test_batch_progress_events"),
        ("TestBranchParallelism", "test_independent_branches_concurrent"),
        ("TestBranchParallelism", "test_results_identical_to_sequential"),
        ("TestBranchParallelism", "test_dataframe_tool_never_concurrent"),
        ("TestBranchParallelism", "test_sequential_engine_deterministic"),
    }
    published = gate.GATES["wetlands-published"]
    assert published.selector == (
        "tests/integration/test_wetlands_smoke.py::test_processing_tool_executes_through_published_wetlands"
    )
    assert published.additional_selectors == ()
    assert set(gate.expected_identities("wetlands-published")).isdisjoint(identities)
    for selected in gate.WETLANDS_GATES:
        with pytest.raises(ValueError, match="Expected one selected"):
            gate.runtime_plan(selected)


@pytest.mark.parametrize("selected,count", [("wetlands-local", 19), ("wetlands-published", 1)])
def test_wetlands_junit_requires_fixed_identities_and_records_source(tmp_path, monkeypatch, selected, count):
    monkeypatch.setenv("GITHUB_SHA", "reviewed-source-sha")
    report = _report(tmp_path / "junit.xml", selected)
    receipt = gate.check_junit(selected, report)
    assert receipt["passed"] == count
    assert receipt["source_sha"] == "reviewed-source-sha"
    assert {(case["classname"], case["name"]) for case in receipt["case_identities"]} == set(
        gate.expected_identities(selected)
    )
    expected = gate.expected_identities(selected)
    for identities in ((), expected + (expected[0],),
                       (*expected[:-1], ("wrong.module", expected[-1][1])),
                       (*expected[:-1], (expected[-1][0], "wrong_case"))):
        _report(report, selected, identities=identities)
        with pytest.raises(ValueError, match="exactly the selected"):
            gate.check_junit(selected, report)


@pytest.mark.parametrize("selected", gate.WETLANDS_GATES)
@pytest.mark.parametrize("effect", ["failure", "error", "skipped"])
def test_wetlands_junit_rejects_failed_or_skipped_case(tmp_path, selected, effect):
    report = _report(tmp_path / "junit.xml", selected, effect=effect)
    with pytest.raises(ValueError, match="failed or skipped"):
        gate.check_junit(selected, report)


@pytest.mark.parametrize("selected,timeout", [("wetlands-local", 3600), ("wetlands-published", 1200)])
def test_wetlands_launcher_uses_fixed_bounded_selectors(tmp_path, monkeypatch, selected, timeout):
    monkeypatch.delenv("BIOIMAGEFLOW_CORE_SOURCE", raising=False)
    report, receipt = tmp_path / "junit.xml", tmp_path / "receipt.json"
    calls = []

    def run(command, *, check, timeout):
        calls.append((command, timeout))
        assert check is False
        _report(report, selected)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.run_pytest(selected, report, receipt) == 0
    selectors = (gate.GATES[selected].selector, *gate.GATES[selected].additional_selectors)
    assert calls[0][0][4:4 + len(selectors)] == list(selectors)
    assert calls[0][1] == timeout
    assert "--run-complete" in calls[0][0]
    assert json.loads(receipt.read_text())["passed"] == (19 if selected == "wetlands-local" else 1)
    with pytest.raises(ValueError, match="Unknown selected"):
        gate.run_pytest("tests/arbitrary.py::test_something", report, receipt)


@pytest.mark.parametrize("source", ["/owned/editable-core", ""])
def test_published_wetlands_launcher_refuses_any_inherited_source_override(tmp_path, monkeypatch, source):
    monkeypatch.setenv("BIOIMAGEFLOW_CORE_SOURCE", source)
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: pytest.fail("Must refuse before pytest"))
    with pytest.raises(ValueError, match="refuses BIOIMAGEFLOW_CORE_SOURCE"):
        gate.run_pytest("wetlands-published", tmp_path / "junit.xml", tmp_path / "receipt.json")
