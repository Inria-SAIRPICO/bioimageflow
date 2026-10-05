"""Controller-local callable evidence and non-executing source admission.

These facts describe supported code/literal state, not arbitrary initializer or
transitive dependency closure. No worker wire format depends on bytecode here.
"""

from __future__ import annotations

import ast
import dis
import hashlib
import inspect
import sys
from types import CodeType, FunctionType, MethodType
from typing import Any, Callable, Mapping

Canonicalizer = Callable[[Any], str]
_MAX_FUNCTIONS = 128


def _literal(value: Any, *, immutable: bool = False, active: set[int] | None = None) -> Any:
    """Close facts to tagged values before the supplied canonicalizer sees them."""
    if value is None:
        return {"type": "none"}
    if value is Ellipsis:
        return {"type": "ellipsis"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": str(value)}
    if type(value) is float:
        return {"type": "float", "value": value.hex()}
    if type(value) is complex:
        return {"type": "complex", "real": value.real.hex(), "imag": value.imag.hex()}
    if type(value) is str:
        return {"type": "str", "value": value}
    if type(value) is bytes:
        return {"type": "bytes", "value": value.hex()}
    allowed = (tuple, frozenset) if immutable else (tuple, list, dict, set, frozenset)
    if type(value) not in allowed:
        raise TypeError("Value is outside closed literal evidence")
    active = set() if active is None else active
    if id(value) in active:
        raise TypeError("Cyclic literal state is not closed evidence")
    active.add(id(value))
    try:
        if type(value) is dict:
            items = [[_literal(k, active=active), _literal(v, active=active)]
                     for k, v in value.items()]
        else:
            items = [_literal(item, immutable=immutable, active=active) for item in value]
        # Set order is canonicalized by the caller after all values are closed.
        return {"type": type(value).__name__, "items": items}
    finally:
        active.remove(id(value))


def _canonical(value: Any, canonicalize: Canonicalizer) -> str:
    def sort_sets(item: Any) -> Any:
        if isinstance(item, dict):
            item = {key: sort_sets(child) for key, child in item.items()}
            if item.get("type") in {"set", "frozenset"}:
                item["items"] = sorted(item["items"], key=canonicalize)
        elif isinstance(item, list):
            item = [sort_sets(child) for child in item]
        return item
    return canonicalize(sort_sets(value))


def _code(code: CodeType) -> dict[str, Any]:
    return {
        "bytecode": code.co_code.hex(),
        "constants": [_code(value) if isinstance(value, CodeType) else _literal(value)
                      for value in code.co_consts],
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "exceptiontable": getattr(code, "co_exceptiontable", b"").hex(),
    }


def _function(callback: Callable[..., Any]) -> FunctionType:
    function = callback.__func__ if isinstance(callback, MethodType) else callback
    if not isinstance(function, FunctionType):
        raise TypeError("Primary callbacks must be Python functions or bound methods")
    return function


def _instructions(code: CodeType):
    yield from dis.get_instructions(code)
    for child in code.co_consts:
        if isinstance(child, CodeType):
            yield from _instructions(child)


def _written_names(function: FunctionType, family: str) -> set[str]:
    return {instruction.argval for instruction in _instructions(function.__code__)
            if instruction.opname in {"STORE_" + family, "DELETE_" + family}}


def _globals(function: FunctionType) -> dict[str, Any]:
    # Nested code executes against the same module globals. Attribute names in
    # co_names are not global reads; use the actual instructions instead.
    names = {instruction.argval for instruction in _instructions(function.__code__)
             if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}}
    return {name: function.__globals__[name] for name in names
            if name in function.__globals__}


def runtime_callable_identity(
    callbacks: Mapping[str, Callable[..., Any]], *, canonicalize: Canonicalizer,
) -> dict[str, Any]:
    """Fingerprint actual callbacks and immutable literal/same-source helpers.

    Mutable observational globals, imported modules and opaque state are named
    unresolved facts; they are not hashed via repr, path, mtime or object ID.
    Retaining the actual bound callbacks is the caller's separate responsibility.
    """
    seen: dict[int, str] = {}
    functions: dict[str, Any] = {}
    unresolved: set[str] = set()

    def literal_fact(value: Any, label: str, *, immutable: bool) -> Any:
        try:
            return _literal(value, immutable=immutable)
        except TypeError:
            unresolved.add(label)
            return {"unresolved": True}

    def visit(function: FunctionType, label: str) -> str:
        if id(function) in seen:
            return seen[id(function)]
        if len(seen) >= _MAX_FUNCTIONS:
            raise ValueError("Callable helper graph exceeds the finite evidence budget")
        seen[id(function)] = label
        facts = {"code": _code(function.__code__),
                 "defaults": literal_fact(function.__defaults__, label + ":defaults", immutable=False),
                 "kwdefaults": literal_fact(function.__kwdefaults__, label + ":kwdefaults", immutable=False)}
        functions[label] = facts
        closure = inspect.getclosurevars(function)
        states = {}
        for family, values, written in (
            ("global", _globals(function), _written_names(function, "GLOBAL")),
            ("closure", closure.nonlocals, _written_names(function, "DEREF")),
        ):
            for name, value in sorted(values.items()):
                key = family + ":" + name
                if name in written:
                    unresolved.add(label + ":" + key)
                    states[key] = {"unresolved": True}
                elif isinstance(value, FunctionType) and value.__code__.co_filename == function.__code__.co_filename:
                    states[key] = {"helper": visit(value, label + "/" + key)}
                else:
                    states[key] = literal_fact(value, label + ":" + key, immutable=True)
        facts["state"] = states
        return label

    roots = {name: visit(_function(callback), "callback:" + name)
             for name, callback in sorted(callbacks.items())}
    identity = {"python": [sys.implementation.name, sys.version_info.major, sys.version_info.minor],
                "callbacks": roots, "functions": functions}
    digest = hashlib.sha256(_canonical(identity, canonicalize).encode("utf-8")).hexdigest()
    return {"digest": digest, "unresolved": tuple(sorted(unresolved))}


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in node.elts))
    return set()


def _binding_index(tree: ast.Module) -> tuple[dict[str, Any], dict[str, ast.AST]]:
    literals: dict[str, Any] = {}
    definitions: dict[str, ast.AST] = {}

    def invalidate(names: set[str], prefix: str = "") -> None:
        for name in names:
            if not prefix:
                literals.pop(name, None)
            key = prefix + name
            for previous in list(definitions):
                if previous == key or previous.startswith(key + "."):
                    definitions.pop(previous)

    def walk(body: list[ast.stmt], prefix: str = "") -> None:
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                invalidate({statement.name}, prefix)
                key = prefix + statement.name
                definitions[key] = statement
                if isinstance(statement, ast.ClassDef):
                    walk(statement.body, key + ".")
            elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                names = set().union(*(_target_names(target) for target in targets))
                invalidate(names, prefix)
                if not prefix and not isinstance(statement, ast.AugAssign) and len(targets) == 1 and isinstance(targets[0], ast.Name) and statement.value is not None:
                    try:
                        literals[targets[0].id] = ast.literal_eval(statement.value)
                    except (ValueError, TypeError):
                        pass
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                names = {alias.asname or alias.name.split(".")[0] for alias in statement.names}
                invalidate(names, prefix)
            elif isinstance(statement, ast.Delete):
                invalidate(set().union(*(_target_names(target) for target in statement.targets)), prefix)
            else:
                # Control-flow bindings cannot establish a final literal/helper.
                names = set()
                for child in ast.walk(statement):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                        names.add(child.id)
                    elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(child.name)
                    elif isinstance(child, (ast.Import, ast.ImportFrom)):
                        names.update(alias.asname or alias.name.split(".")[0] for alias in child.names)
                    elif isinstance(child, ast.ExceptHandler) and child.name:
                        names.add(child.name)
                    elif type(child).__name__ in {"MatchAs", "MatchStar", "MatchMapping"}:
                        bound = getattr(child, "name", None) or getattr(child, "rest", None)
                        if bound:
                            names.add(bound)
                invalidate(names, prefix)
    walk(tree.body)
    return literals, definitions


def _code_index(code: CodeType, prefix: str = "") -> dict[str, CodeType]:
    result: dict[str, CodeType] = {}
    for child in code.co_consts:
        if isinstance(child, CodeType):
            key = prefix + child.co_name
            result[key] = child
            result.update(_code_index(child, key + "."))
    return result


def validate_source_callables(
    source: bytes, callbacks: Mapping[str, Callable[..., Any]], *, canonicalize: Canonicalizer,
) -> tuple[str, ...]:
    """Refuse supported resident/source code, literal or default mismatches.

    Parsing/compilation never executes module initializers or imports. Matching
    is controller-local; its bytecode is not a cross-interpreter worker token.
    Returns explicit unknown initializer/dependency facts rather than claiming
    that matching primary code proves complete executable closure.
    """
    tree = ast.parse(source)
    literals, definitions = _binding_index(tree)
    codes = _code_index(compile(tree, "<admitted-source>", "exec", dont_inherit=True))
    unresolved: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            unresolved.add("module:import-dependencies")
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            if statement.value is None:
                unresolved.add("module:initializer")
                continue
            try:
                ast.literal_eval(statement.value)
            except (ValueError, TypeError):
                unresolved.add("module:initializer")
        elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)):
                unresolved.add("module:initializer")
    seen: set[int] = set()

    def compare(actual: Any, expected: Any, label: str, *, immutable: bool = False) -> None:
        try:
            left = _literal(actual, immutable=immutable)
            right = _literal(expected, immutable=immutable)
        except TypeError:
            unresolved.add(label)
            return
        if _canonical(left, canonicalize) != _canonical(right, canonicalize):
            raise ValueError("Resident/source literal mismatch: " + label)

    def visit(function: FunctionType) -> None:
        if id(function) in seen:
            return
        if len(seen) >= _MAX_FUNCTIONS:
            raise ValueError("Callable helper graph exceeds the finite evidence budget")
        seen.add(id(function))
        name = function.__qualname__.replace(".<locals>", "")
        declaration = definitions.get(name)
        expected = codes.get(name)
        if not isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef)) or expected is None:
            unresolved.add(name + ":source-declaration")
            return
        if _canonical(_code(function.__code__), canonicalize) != _canonical(_code(expected), canonicalize):
            raise ValueError("Resident/source callable mismatch: " + name)
        if declaration.decorator_list:
            unresolved.add(name + ":decorators")
        try:
            defaults = tuple(ast.literal_eval(item) for item in declaration.args.defaults) or None
            compare(function.__defaults__, defaults, name + ":defaults")
        except (ValueError, TypeError) as error:
            if isinstance(error, ValueError) and str(error).startswith("Resident/source"):
                raise
            unresolved.add(name + ":defaults")
        try:
            kwdefaults = {argument.arg: ast.literal_eval(value)
                          for argument, value in zip(declaration.args.kwonlyargs, declaration.args.kw_defaults)
                          if value is not None} or None
            compare(function.__kwdefaults__, kwdefaults, name + ":kwdefaults")
        except (ValueError, TypeError) as error:
            if isinstance(error, ValueError) and str(error).startswith("Resident/source"):
                raise
            unresolved.add(name + ":kwdefaults")
        closure = inspect.getclosurevars(function)
        written = _written_names(function, "GLOBAL")
        for key, value in sorted(_globals(function).items()):
            if key in written:
                unresolved.add(name + ":global:" + key)
            elif isinstance(value, FunctionType) and value.__code__.co_filename == function.__code__.co_filename:
                visit(value)
            elif key in literals:
                compare(value, literals[key], name + ":global:" + key, immutable=True)
            else:
                unresolved.add(name + ":global:" + key)
        for key in closure.nonlocals:
            unresolved.add(name + ":closure:" + key)

    for callback in callbacks.values():
        visit(_function(callback))
    return tuple(sorted(unresolved))
