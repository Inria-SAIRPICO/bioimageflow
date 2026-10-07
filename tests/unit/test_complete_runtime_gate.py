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


def _report(path, selected, *, effect=None, names=None, classname=None):
    filename, function = gate.GATES[selected].selector.split("::")
    expected = (
        [f"{function}[{case}]" for case in gate.GATES[selected].cases]
        if gate.GATES[selected].cases else [function]
    )
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests=str(len(expected)))
    for name in expected if names is None else names:
        case = ET.SubElement(
            suite, "testcase", name=name,
            classname=classname or filename.removesuffix(".py").replace("/", "."),
        )
        if effect:
            ET.SubElement(case, effect)
    ET.ElementTree(root).write(path)
    return path


def test_complete_selection_keeps_defaults_and_refuses_incompatible_target():
    gate.validate_selection("all", "all", "schedule")
    gate.validate_selection("external-binaries", "all")
    for selected in gate.RUNTIME_CHOICES[1:]:
        gate.validate_selection("model-runtimes", selected)
        with pytest.raises(ValueError):
            gate.validate_selection("all", selected)
        with pytest.raises(ValueError):
            gate.validate_selection("model-runtimes", selected, "schedule")
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
    for selected in gate.RUNTIME_CHOICES[1:]:
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
