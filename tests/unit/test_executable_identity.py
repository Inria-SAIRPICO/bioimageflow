"""Actual callable/source distinctions, without module execution by admission."""

import json
from types import ModuleType

import pytest

from bioimageflow_core.executable_identity import (
    runtime_callable_identity,
    validate_source_callables,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _module(source, filename="/admitted/tool.py"):
    module = ModuleType("admitted_tool")
    exec(compile(source, filename, "exec", dont_inherit=True), module.__dict__)
    return module


def _identity(callback):
    return runtime_callable_identity({"transform": callback}, canonicalize=_canonical)


def test_actual_nested_constants_and_typed_values_change_identity():
    one = _module("def transform():\n def nested(): return 2**63+1\n return nested()\n")
    two = _module("def transform():\n def nested(): return 2**63+2\n return nested()\n")
    assert one.transform() == 2**63 + 1
    assert two.transform() == 2**63 + 2
    assert _identity(one.transform)["digest"] != _identity(two.transform)["digest"]
    boolean = _module("def transform(): return True\n")
    integer = _module("def transform(): return 1\n")
    assert _identity(boolean.transform)["digest"] != _identity(integer.transform)["digest"]


def test_identity_ignores_source_path_line_locations_and_observer_mutation():
    source = "CALLS=[]\ndef transform():\n CALLS.append('called')\n return 4\n"
    one = _module(source, "/first/tool.py")
    two = _module("\n\n" + source, "/second/tool.py")
    before = _identity(one.transform)
    assert one.transform() == 4
    assert before == _identity(one.transform) == _identity(two.transform)
    assert any("global:CALLS" in item for item in before["unresolved"])


@pytest.mark.parametrize("kind", ["global", "helper"])
def test_actual_literal_and_helper_changes_do_not_require_primary_code_change(kind):
    prefix = "VALUE=4\n" if kind == "global" else "def helper(): return 4\n"
    expression = "VALUE" if kind == "global" else "helper()"
    source = prefix + "def transform(): return " + expression + "\n"
    module = _module(source)
    before = _identity(module.transform)
    if kind == "global":
        module.VALUE = 99
    else:
        exec(compile("def helper(): return 99\n", "/admitted/tool.py", "exec", dont_inherit=True), module.__dict__)
    assert module.transform() == 99
    assert _identity(module.transform)["digest"] != before["digest"]


@pytest.mark.parametrize("kind", ["global", "helper"])
def test_nested_code_captures_actual_global_and_helper_facts(kind):
    prefix = "VALUE=4\n" if kind == "global" else "def helper(): return 4\n"
    expression = "VALUE" if kind == "global" else "helper()"
    source = prefix + "def transform():\n def nested(): return " + expression + "\n return nested()\n"
    one = _module(source)
    changed = source.replace("return 4", "return 99") if kind == "helper" else source.replace("VALUE=4", "VALUE=99")
    two = _module(changed)
    assert one.transform() == 4 and two.transform() == 99
    assert _identity(one.transform)["digest"] != _identity(two.transform)["digest"]
    with pytest.raises(ValueError, match="Resident/source"):
        validate_source_callables(changed.encode(), {"transform": one.transform}, canonicalize=_canonical)


def test_defaults_and_closed_closure_are_actual_semantic_state():
    def make(value):
        def transform(argument=(1, b"a"), *, scale=2):
            return value + scale + argument[0]
        return transform
    one = make(4)
    two = make(9)
    assert _identity(one)["digest"] != _identity(two)["digest"]
    before = _identity(one)
    one.__kwdefaults__ = {"scale": 3}
    assert _identity(one)["digest"] != before["digest"]


def test_recursive_helper_graph_is_bounded_without_object_ids_in_facts():
    source = "def helper(n): return 0 if not n else transform(n-1)\ndef transform(n=1): return helper(n)\n"
    one = _module(source, "/first/tool.py")
    two = _module(source, "/second/tool.py")
    assert one.transform() == two.transform() == 0
    assert _identity(one.transform) == _identity(two.transform)


@pytest.mark.parametrize("changed", [
    "VALUE=9\ndef helper(): return VALUE\nclass Tool:\n def transform(self, x=2): return helper()+x\n",
    "VALUE=4\ndef helper(): return VALUE+1\nclass Tool:\n def transform(self, x=2): return helper()+x\n",
    "VALUE=4\ndef helper(): return VALUE\nclass Tool:\n def transform(self, x=3): return helper()+x\n",
    "VALUE=4\ndef helper(): return VALUE\nclass Tool:\n def transform(self, x=2): return helper()-x\n",
])
def test_managed_source_refuses_literal_helper_default_or_body_mismatch(changed):
    source = "VALUE=4\ndef helper(): return VALUE\nclass Tool:\n def transform(self, x=2): return helper()+x\n"
    tool = _module(source).Tool()
    assert validate_source_callables(source.encode(), {"transform": tool.transform}, canonicalize=_canonical) == ()
    with pytest.raises(ValueError, match="Resident/source"):
        validate_source_callables(changed.encode(), {"transform": tool.transform}, canonicalize=_canonical)
    assert tool.transform() == 6


@pytest.mark.parametrize("rebind", ["VALUE=dynamic()", "if condition:\n VALUE=9", "import other as VALUE",
                                    "if condition:\n import other as VALUE",
                                    "if condition:\n from other import value as VALUE"])
def test_dynamic_final_rebinding_invalidates_prior_literal_without_execution(rebind):
    original = "VALUE=4\ndef transform(): return VALUE\n"
    callback = _module(original).transform
    source = "VALUE=9\n" + rebind + "\ndef transform(): return VALUE\n"
    unresolved = validate_source_callables(source.encode(), {"transform": callback}, canonicalize=_canonical)
    assert "transform:global:VALUE" in unresolved
    assert callback() == 4


def test_dynamic_helper_rebinding_is_unknown_not_stale_known_helper():
    module = _module("def helper(): return 4\ndef transform(): return helper()\n")
    source = b"def helper(): return 9\nhelper=dynamic()\ndef transform(): return helper()\n"
    unresolved = validate_source_callables(source, {"transform": module.transform}, canonicalize=_canonical)
    assert "helper:source-declaration" in unresolved
    assert module.transform() == 4


def test_source_admission_does_not_execute_initializers_or_imports(tmp_path):
    marker = tmp_path / "should-not-exist"
    module = _module("def transform(): return 4\n")
    source = ("from missing_uninstalled_package import unavailable\n"
              + f"open({str(marker)!r}, 'w').write('effect')\n"
              + "def transform(): return 4\n")
    assert set(validate_source_callables(source.encode(), {"transform": module.transform}, canonicalize=_canonical)) == {
        "module:import-dependencies", "module:initializer",
    }
    assert not marker.exists()


def test_literal_facts_are_closed_before_custom_canonicalizer():
    def transform(value=(b"bytes", complex(1, 2), float("inf"), 2**64-1)):
        return value
    evidence = _identity(transform)
    assert len(evidence["digest"]) == 64
    assert evidence["unresolved"] == ()
