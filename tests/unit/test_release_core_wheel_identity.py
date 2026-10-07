"""Publication binds Core to the reviewed wheel before uploading any package."""

import hashlib
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts import release_set
from scripts.release_support import Package, ReleaseError
from tests.unit.test_release_tooling import _workflow, _write_artifacts


ROOT = Path(__file__).parents[2]


def _prepare(tmp_path, *, core=True):
    packages = [Package("a-independent", "1.0.0", tmp_path / "independent")]
    if core:
        packages.append(Package("bioimageflow-core", "0.5.0", tmp_path / "core"))
    for package in packages:
        _write_artifacts(tmp_path / f"release-{package.name}-{package.version}", package)
    items = tuple(release_set.ReleaseItem(package, package.version, package.release_tag)
                  for package in packages)
    plan = release_set.ReleasePlan("a" * 40, items, tuple(package.name for package in packages))
    wheel = (tmp_path / "release-bioimageflow-core-0.5.0"
             / "bioimageflow_core-0.5.0-py3-none-any.whl") if core else None
    return plan, wheel


def _runner(calls):
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    return run


def test_reviewed_core_wheel_and_independent_package_publish_unchanged(tmp_path):
    plan, wheel = _prepare(tmp_path)
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    calls = []
    assert release_set.publish_release_set(
        plan, tmp_path, expected_core_sha256=expected, runner=_runner(calls),
    ) == ["a-independent", "bioimageflow-core"]
    assert len(calls) == 2
    assert str(wheel) in calls[1]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("expected", ["", "a" * 63, "A" * 64, "z" * 64])
def test_selected_core_requires_a_valid_reviewed_hash_before_any_upload(tmp_path, expected):
    plan, _ = _prepare(tmp_path)
    calls = []
    with pytest.raises(ReleaseError, match="reviewed canonical wheel SHA256"):
        release_set.publish_release_set(
            plan, tmp_path, expected_core_sha256=expected, runner=_runner(calls),
        )
    assert calls == []


def test_same_name_version_rebuilt_core_is_refused_before_independent_upload(tmp_path):
    plan, wheel = _prepare(tmp_path)
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
        archive.writestr("bioimageflow_core/changed.py", "value = 9\n")
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() != expected
    calls = []
    with pytest.raises(ReleaseError, match="Staged Core wheel differs"):
        release_set.publish_release_set(
            plan, tmp_path, expected_core_sha256=expected, runner=_runner(calls),
        )
    assert calls == []


def test_non_core_release_does_not_require_core_authority(tmp_path):
    plan, _ = _prepare(tmp_path, core=False)
    calls = []
    assert release_set.publish_release_set(plan, tmp_path, runner=_runner(calls)) == ["a-independent"]
    assert len(calls) == 1


def test_publish_cli_and_workflow_pass_the_reviewed_core_hash(tmp_path, monkeypatch):
    plan, wheel = _prepare(tmp_path)
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(release_set, "_plan_from_args", lambda args: plan)
    calls = []

    def publish(selected, root, *, expected_core_sha256):
        calls.append((selected, root, expected_core_sha256))
        return list(selected.publish_order)

    monkeypatch.setattr(release_set, "publish_release_set", publish)
    assert release_set.main([
        "publish", "--artifacts-dir", str(tmp_path), "--expected-core-sha256", expected,
        *(item.tag for item in plan.items),
    ]) == 0
    assert calls == [(plan, tmp_path, expected)]
    workflow = _workflow(ROOT, "release.yml")
    field = workflow["on"]["workflow_dispatch"]["inputs"]["expected_core_sha256"]
    assert field["default"] == "" and field["required"] == "false"
    step = next(step for step in workflow["jobs"]["publish"]["steps"]
                if step["name"] == "Publish packages in dependency order")
    assert step["env"]["EXPECTED_CORE_SHA256"] == "${{ inputs.expected_core_sha256 }}"
    assert '--expected-core-sha256 "$EXPECTED_CORE_SHA256"' in step["run"]
